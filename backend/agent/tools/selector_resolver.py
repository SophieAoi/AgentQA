"""
Mechanical fallback chain for resolving a natural-language selector hint
(e.g. "the Save button", coming from the Executor's tool calls) into a real
Playwright locator, without invoking an LLM for the common case.

See docs/BUILD-PLAN.md § "Self-healing selectors: mechanical fallback chain
first" for the design rationale. Tiers, in order:

1. primary             — exact accessible-name match via role/label/placeholder/text
2. fallback_role        — same idea, relaxed to a partial (non-exact) name match
3. fallback_keyword     — hint's significant word(s) (filler stripped) matched
                           against each candidate element's accessible name —
                           catches hints like "the Email field" against a real
                           label of "Email / Username *", which fallback_role's
                           plain substring check can't: neither string contains
                           the other verbatim, only the shared keyword "email"
                           does. Still requires exactly one match, same
                           precision discipline as every other mechanical tier.
4. fallback_nearby_label — for a genuinely unlabeled input (no aria-label, no
                           <label for=>/id association at all — confirmed live
                           on the real Influence login form: a <label> sits as
                           a plain sibling with zero programmatic link to its
                           <input>), tiers 1-3 have nothing to match against
                           since the label text never becomes part of the
                           input's accessible name. Finds the nearest
                           following <label>/text node containing the hint's
                           keyword in DOM order and resolves to the input that
                           immediately follows THAT label — this is a genuine
                           site accessibility gap, not something fixable by
                           rewording test case hints (a hint rewrite would
                           just be one specific patch tied to today's exact
                           placeholder text, breaking again on the next
                           copy change).
5. fallback_fuzzy       — broad, case-insensitive substring text match anywhere
6. fallback_cache       — a (role, name) pair that resolved this exact hint on a
                           prior run of the same test case, tried even if it
                           isn't a role normally associated with this action type
7. llm_selected         — not resolved here at all: returning (None, ...) tells
                           the Executor every mechanical tier failed, so it
                           makes one LLM call with the current DOM snapshot
                           instead.

Every tier actually used gets logged by the caller onto ExecutionStep — real
breakage-rate telemetry, not a guess, is what should eventually justify (or
rule out) investing in fuller LLM-based DOM analysis.
"""

import re
from typing import Optional

from playwright.async_api import Locator, Page

from app.models.schemas import SelectorStrategy

ACTION_ROLES: dict[str, list[str]] = {
    "click": ["button", "link", "checkbox", "radio", "option", "tab", "menuitem"],
    "fill": ["textbox"],
    "select_option": ["combobox", "listbox"],
}

# Words that describe the *kind* of element or the *action* being taken,
# rather than identifying WHICH element — stripping them from a hint like
# "the Email field" leaves "email", the actual keyword that needs to
# appear in the real accessible name. Deliberately conservative: only
# generic filler/role/verb words that add no identifying information,
# never anything that could itself be part of a real label (so "confirm
# password field" still keeps "confirm password").
_HINT_FILLER_WORDS = {
    "the", "a", "an", "this", "that",
    "field", "fields", "input", "box", "button", "link", "icon",
    "dropdown", "menu", "checkbox", "toggle", "option", "tab",
    "click", "select", "choose", "enter", "fill", "check", "uncheck", "on", "in",
}

# A keyword shorter than this is too generic on its own to safely identify
# one element (e.g. "in" from "Sign In", "ok" from "OK button") — matching
# it as a substring risks silently landing on the wrong element rather
# than correctly falling through to a slower but safer tier.
_MIN_KEYWORD_LENGTH = 3


def _hint_keywords(hint: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", hint.lower())
    return [w for w in words if w not in _HINT_FILLER_WORDS and len(w) >= _MIN_KEYWORD_LENGTH]


class SelectorCache:
    """
    Per-process cache of (test_case_id, hint) -> the (role, name) pair that
    last resolved it successfully. Deliberately the *last* mechanical tier,
    not the first — the live DOM is more trustworthy than a possibly-stale
    cached selector, but a cache hit is still cheaper and more specific than
    an LLM call.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], tuple[str, str]] = {}

    def get(self, test_case_id: str, hint: str) -> Optional[tuple[str, str]]:
        return self._cache.get((test_case_id, hint))

    def set(self, test_case_id: str, hint: str, role: str, name: str) -> None:
        self._cache[(test_case_id, hint)] = (role, name)


async def _count(locator: Locator) -> int:
    try:
        return await locator.count()
    except Exception:
        return 0


async def resolve_selector(
    page: Page,
    hint: str,
    action_type: str,
    test_case_id: str,
    cache: SelectorCache,
) -> tuple[Optional[Locator], SelectorStrategy, Optional[str]]:
    """
    Returns (locator, strategy, description) on success, or
    (None, SelectorStrategy.llm_selected, None) if every mechanical tier
    failed — the sentinel telling the Executor to fall back to an LLM call.
    """
    roles = ACTION_ROLES.get(action_type, [])

    # Tier 1: primary — exact match
    for role in roles:
        loc = page.get_by_role(role, name=hint, exact=True)
        if await _count(loc) == 1:
            cache.set(test_case_id, hint, role, hint)
            return loc, SelectorStrategy.primary, f"role={role}[name={hint!r} exact]"

    for description, candidate in (
        ("label", page.get_by_label(hint, exact=True)),
        ("placeholder", page.get_by_placeholder(hint, exact=True)),
        ("text", page.get_by_text(hint, exact=True)),
    ):
        if await _count(candidate) == 1:
            return candidate, SelectorStrategy.primary, f"{description}={hint!r} exact"

    # Tier 2: fallback_role — relaxed to a partial accessible-name match
    for role in roles:
        loc = page.get_by_role(role, name=hint, exact=False)
        if await _count(loc) == 1:
            cache.set(test_case_id, hint, role, hint)
            return loc, SelectorStrategy.fallback_role, f"role={role}[name~={hint!r}]"

    # Tier 3: fallback_keyword — Tier 2 failed because the full hint isn't
    # a substring of the real name in either direction (e.g. "the Email
    # field" vs. "Email / Username *" — "the"/"field" are noise neither
    # side has). Stripping filler words down to the significant keyword(s)
    # and re-trying Playwright's own substring name-match per keyword
    # catches this without a full LLM round trip. Each keyword must
    # resolve to exactly one element to stay safe — a keyword vague enough
    # to match multiple elements is treated as inconclusive, not guessed.
    keywords = _hint_keywords(hint)
    for keyword in keywords:
        for role in roles:
            loc = page.get_by_role(role, name=keyword, exact=False)
            if await _count(loc) == 1:
                cache.set(test_case_id, hint, role, keyword)
                return loc, SelectorStrategy.fallback_keyword, f"role={role}[name~={keyword!r}] (from hint {hint!r})"

    # Tier 4: fallback_nearby_label — only meaningful for text-entry actions;
    # a click/select target's accessible name IS its visible label in every
    # real case seen so far, so this tier is scoped to "fill" to avoid ever
    # matching a click hint against unrelated nearby label text.
    if action_type == "fill":
        for keyword in keywords:
            escaped = keyword.replace("'", "")  # keywords are alphanumeric-only (see _hint_keywords), belt-and-suspenders
            loc = page.locator(
                f"xpath=//label[contains(translate(text(), "
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{escaped}')]"
                f"/following::input[1]"
            )
            if await _count(loc) == 1:
                return (
                    loc,
                    SelectorStrategy.fallback_nearby_label,
                    f"input following label~={keyword!r} (from hint {hint!r})",
                )

    # Tier 5: fallback_fuzzy — broad, case-insensitive substring match anywhere
    fuzzy = page.get_by_text(hint, exact=False)
    if await _count(fuzzy) >= 1:
        return fuzzy.first, SelectorStrategy.fallback_fuzzy, f"fuzzy text~={hint!r}"

    # Tier 6: fallback_cache — retry a (role, name) pair that worked before,
    # even for a role this action type wouldn't normally try.
    cached = cache.get(test_case_id, hint)
    if cached:
        role, name = cached
        loc = page.get_by_role(role, name=name, exact=False)
        if await _count(loc) >= 1:
            return loc.first, SelectorStrategy.fallback_cache, f"cached role={role}[name~={name!r}]"

    return None, SelectorStrategy.llm_selected, None
