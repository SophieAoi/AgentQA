from pathlib import Path

from agent.browser.session import BrowserSession
from agent.tools.selector_resolver import SelectorCache, resolve_selector
from app.models.schemas import SelectorStrategy

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
INTERACTIVE_PAGE = f"file://{FIXTURES_DIR / 'interactive_page.html'}"
AMBIGUOUS_KEYWORD_PAGE = f"file://{FIXTURES_DIR / 'ambiguous_keyword_page.html'}"
UNLABELED_INPUT_PAGE = f"file://{FIXTURES_DIR / 'unlabeled_input_page.html'}"


async def test_tier_1_primary_exact_role_match():
    async with BrowserSession(headless=True) as session:
        await session.goto(INTERACTIVE_PAGE)
        locator, strategy, description = await resolve_selector(
            session.page, "Sign In", "click", "TC-TEST", SelectorCache()
        )
        assert locator is not None
        assert strategy == SelectorStrategy.primary
        assert "Sign In" in description


async def test_tier_2_fallback_role_partial_match():
    async with BrowserSession(headless=True) as session:
        await session.goto(INTERACTIVE_PAGE)
        # "Confirm" is not the exact accessible name of any button (the only
        # match is "Confirm Delete"), so tier 1 must fail and tier 2's
        # partial-match role search must be what actually resolves it.
        locator, strategy, description = await resolve_selector(
            session.page, "Confirm", "click", "TC-TEST", SelectorCache()
        )
        assert locator is not None
        assert strategy == SelectorStrategy.fallback_role


async def test_tier_3_fallback_keyword_match():
    """
    Regression test: observed live — a hint like "the Email field" never
    matches a real accessible name of "Email / Username *" via tier 2's
    substring check, since neither string contains the other verbatim
    ("the"/"field" are noise the real name doesn't have). This forced
    every login-suite fill() call through the expensive LLM-selection
    fallback. Stripping filler words down to the keyword "email" and
    re-trying a substring match against just that keyword is what tier 3
    now catches this with.
    """
    async with BrowserSession(headless=True) as session:
        await session.goto(INTERACTIVE_PAGE)
        locator, strategy, description = await resolve_selector(
            session.page, "the Email field", "fill", "TC-TEST", SelectorCache()
        )
        assert locator is not None
        assert strategy == SelectorStrategy.fallback_keyword
        assert "email" in description.lower()


async def test_tier_3_skips_a_keyword_that_matches_multiple_elements():
    """
    Safety guard: if a stripped-down keyword is ambiguous (matches more
    than one textbox), tier 3 must not guess which one — both the real
    "Email / Username *" field and the fixture's unrelated placeholder
    field share the word "field" only in the hint, not in their names, but
    "username" would false-match nothing else either; instead this checks
    a keyword that genuinely appears in two different accessible names
    ("Email" appears in both "Email / Username *" and a second decoy
    field added just for this test) resolves to neither mechanically,
    correctly falling through to the LLM tier rather than picking one.
    """
    async with BrowserSession(headless=True) as session:
        await session.goto(AMBIGUOUS_KEYWORD_PAGE)
        locator, strategy, description = await resolve_selector(
            session.page, "the Email field", "fill", "TC-TEST", SelectorCache()
        )
        assert locator is None
        assert strategy == SelectorStrategy.llm_selected


async def test_tier_4_fallback_nearby_label_match():
    """
    Regression test: observed live against the real Influence login page —
    a <label> sits as a plain sibling of its <input> with no `for=`/id
    association and no aria-label, so the label text never becomes part of
    the input's accessible name at all. Tiers 1-3 (all accessible-name-
    based) have nothing to match "the Email field" against; only walking
    the DOM for a nearby label containing the keyword and resolving to the
    input immediately following it can find this field mechanically.
    """
    async with BrowserSession(headless=True) as session:
        await session.goto(UNLABELED_INPUT_PAGE)
        locator, strategy, description = await resolve_selector(
            session.page, "the Email field", "fill", "TC-TEST", SelectorCache()
        )
        assert locator is not None
        assert strategy == SelectorStrategy.fallback_nearby_label
        assert "email" in description.lower()
        assert await locator.get_attribute("placeholder") == "Ex : you@company.com"


async def test_tier_4_fallback_nearby_label_distinguishes_password_field():
    async with BrowserSession(headless=True) as session:
        await session.goto(UNLABELED_INPUT_PAGE)
        locator, strategy, description = await resolve_selector(
            session.page, "the Password field", "fill", "TC-TEST", SelectorCache()
        )
        assert locator is not None
        assert strategy == SelectorStrategy.fallback_nearby_label
        assert await locator.get_attribute("type") == "password"


async def test_tier_4_fallback_nearby_label_only_applies_to_fill():
    """
    Scoped to "fill" deliberately — a click/select target's accessible name
    IS its visible label in every real case seen so far, so this tier
    should never fire for a click hint even if a nearby label happens to
    contain the same keyword (that would risk matching an unrelated
    input instead of the intended clickable element).
    """
    async with BrowserSession(headless=True) as session:
        await session.goto(UNLABELED_INPUT_PAGE)
        locator, strategy, description = await resolve_selector(
            session.page, "the Email field", "click", "TC-TEST", SelectorCache()
        )
        assert strategy != SelectorStrategy.fallback_nearby_label


async def test_tier_5_fallback_fuzzy_text_match():
    async with BrowserSession(headless=True) as session:
        await session.goto(INTERACTIVE_PAGE)
        # No role="button"/etc. applies to a bare <span> — role-based tiers
        # 1, 2, and 3 can't find it, only the broad text search here can.
        locator, strategy, description = await resolve_selector(
            session.page, "Fuzzy Marker", "click", "TC-TEST", SelectorCache()
        )
        assert locator is not None
        assert strategy == SelectorStrategy.fallback_fuzzy


async def test_tier_6_fallback_cache_used_when_live_tiers_fail():
    async with BrowserSession(headless=True) as session:
        await session.goto(INTERACTIVE_PAGE)
        cache = SelectorCache()
        # Seed the cache as if a prior run had resolved "the confirmation
        # button" to the real "Confirm Delete" button — a hint with no
        # literal relationship to the page text, so tiers 1-3 can't find it.
        cache.set("TC-TEST", "the confirmation button", "button", "Confirm Delete")

        locator, strategy, description = await resolve_selector(
            session.page, "the confirmation button", "click", "TC-TEST", cache
        )
        assert locator is not None
        assert strategy == SelectorStrategy.fallback_cache


async def test_returns_llm_selected_sentinel_when_nothing_matches():
    async with BrowserSession(headless=True) as session:
        await session.goto(INTERACTIVE_PAGE)
        locator, strategy, description = await resolve_selector(
            session.page, "a completely nonexistent widget", "click", "TC-TEST", SelectorCache()
        )
        assert locator is None
        assert strategy == SelectorStrategy.llm_selected
        assert description is None


async def test_cache_is_populated_after_a_primary_tier_success():
    async with BrowserSession(headless=True) as session:
        await session.goto(INTERACTIVE_PAGE)
        cache = SelectorCache()
        await resolve_selector(session.page, "Sign In", "click", "TC-TEST", cache)
        assert cache.get("TC-TEST", "Sign In") == ("button", "Sign In")
