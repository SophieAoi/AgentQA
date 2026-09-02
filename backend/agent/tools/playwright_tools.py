"""
The Executor's explicit Playwright tool set: click, fill, select_option,
navigate, read_page, assert_condition. Deliberately a small, fixed set of
dedicated tools rather than a raw bash/eval escape hatch — see
docs/BUILD-PLAN.md's linked agent-design guidance on promoting actions to
dedicated tools when the harness needs to audit or gate them, which every
one of these does (each call becomes a logged, screenshot-able
ExecutionStep).

build_tools(ctx) returns the six @local_tool-decorated closures bound
to one ToolContext (one page, one run, one test case) for the tool loop
(agent/local_llm.py::run_tool_loop) to call directly.
"""

import asyncio
import base64
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from playwright.async_api import Locator, Page
from pydantic import BaseModel

from agent.local_llm import LocalLLMError, local_tool, structured_chat
from app.models.schemas import ExecutionStep, SelectorStrategy, StepType, TestRunStatus
from app.services.event_bus import EventBus
from app.services.store import StoreProtocol
from agent.tools.redaction import is_sensitive_hint, redact_tool_input, resolve_credential_placeholder
from agent.tools.selector_resolver import SelectorCache, resolve_selector

MAX_SNAPSHOT_CHARS = 4000

# How long the cursor overlay stays visible before the real action runs —
# long enough for a human watching the live stream to actually perceive
# it (anything much under this reads as an imperceptible flicker), short
# enough not to meaningfully slow down a test run.
CURSOR_VISIBLE_SECONDS = 0.6


@dataclass
class ToolContext:
    page: Page
    store: StoreProtocol
    run_id: str
    test_case_id: str
    selector_cache: SelectorCache
    log: callable
    event_bus: EventBus
    _step_index: int = 0
    steps: list[ExecutionStep] = field(default_factory=list)
    # Latest real DOM read (assert_condition/read_page) — what the verifier
    # judges an assertion step's expected_outcome against.
    last_snapshot: Optional[str] = None
    # Snapshot captured immediately after the most recent click/fill/
    # select_option, separate from last_snapshot. On at least one real
    # page, a click's result is a toast that renders within ~1-2s and
    # self-clears within ~6s — the model's own turn-taking time before it
    # gets around to calling assert_condition can exceed that window, so
    # by the time last_snapshot is captured the toast may already be gone.
    # Keeping this one separate means that evidence isn't lost just
    # because a later read overwrote last_snapshot with a staler page
    # state — see evaluate() in agent/verifier.py, which is given both.
    post_action_snapshot: Optional[str] = None

    def _next_step_index(self) -> int:
        self._step_index += 1
        return self._step_index


# Confirmed live, repeatedly (docs/phase-14-blank-field-steps-and-merged-
# text-bias.md): Playwright's aria_snapshot() concatenates adjacent
# unlabeled text nodes onto one "text:" line with just a space and no
# other separator, even when they're unrelated parts of the real page —
# most commonly a validation message immediately followed by the NEXT
# field's own "required" marker, e.g. "Email / Username is required
# Password *" (two fields' worth of independent text on one line) or
# "Start date is required End date is required" (same shape, no
# trailing asterisk). Three rounds of verifier-prompt wording failed to
# stop the model from treating this as grounds to fail an otherwise-
# correct check; a first attempt at fixing it in the snapshot itself was
# reverted after it also split a legitimate single label ("Email /
# Username *" alone) that merely happens to end the same way. This
# version is deliberately much narrower: it only splits directly after
# the literal word "required", and only when what immediately follows
# also looks like another short requirement/label phrase (ends in
# " *" or contains its own "is/are required") — never inside an
# ordinary label or sentence, including ones that end in " *" or
# contain "required" with nothing label-shaped following it. Verified
# against real captured snapshots from the login form, dashboard,
# campaigns list, and line-item creation form before shipping — see the
# phase-14 doc for the exact cases this was checked against.
_MERGED_REQUIRED_SPLIT_RE = re.compile(
    r"(?<=required) (?=[A-Za-z][A-Za-z /]*?(?:\*(?:\s|$)|\bis required\b|\bare required\b))"
)


def _split_merged_required_text(snapshot: str) -> str:
    lines = snapshot.split("\n")
    fixed = []
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith("- text:"):
            prefix, content = indent + "- text:", stripped[len("- text:") :]
        elif stripped.startswith("text:"):
            prefix, content = indent + "text:", stripped[len("text:") :]
        else:
            fixed.append(line)
            continue
        content = content.strip()
        parts = _MERGED_REQUIRED_SPLIT_RE.split(content)
        if len(parts) <= 1:
            fixed.append(line)
            continue
        fixed.extend(f"{prefix} {part}" for part in parts)
    return "\n".join(fixed)


async def _page_snapshot(page: Page) -> str:
    """A trimmed, LLM-friendly YAML accessibility snapshot — not raw HTML,
    to keep tool-result tokens bounded. Prefixed with the current URL and
    page <title>: neither is part of the body's accessibility tree that
    aria_snapshot() reads, so without adding them explicitly, an assertion
    like "verify the page title is present/correct" can never be judged
    from real evidence — the Verifier isn't reasoning about the title
    poorly, the title simply never reaches it otherwise (same gap the URL
    prefix below already closed for URL-based assertions)."""
    try:
        title = await page.title()
    except Exception:
        title = "(could not read title)"
    try:
        snapshot = await page.locator("body").aria_snapshot(timeout=5000)
    except Exception as exc:
        return f"(could not read page: {exc})"
    snapshot = _split_merged_required_text(snapshot)
    return f"Current URL: {page.url}\nPage title: {title}\n\n{snapshot}"[:MAX_SNAPSHOT_CHARS]


_CURSOR_OVERLAY_ID = "__agentqa_cursor_overlay__"

# Injected as a fixed-position element positioned at the target's
# coordinates just before a click/fill screenshot is taken, then removed
# right after — Playwright drives the real browser via the DevTools
# protocol, which dispatches events directly with no OS-level mouse
# movement or rendered cursor at all, so there's nothing to screenshot
# without drawing one in ourselves. A CSS-only cursor "hand" glyph plus a
# brief ripple keeps this to a single injected <style>+<div>, no image
# asset, and self-cleans so it never lingers into a real page snapshot or
# affects selector resolution.
# How long the CSS-driven glide from the cursor's previous position to a
# new target takes. Matched by _show_cursor_at's real-time sleep/screenshot
# loop below — a transition alone would be invisible on the live stream,
# since a viewer only ever sees whatever single screenshot happens to be
# taken (page.screenshot() captures one static instant, not video), so
# without capturing multiple frames DURING this window the cursor would
# still look like it teleported between two static endpoints regardless of
# the CSS. Short enough to not meaningfully add to per-action latency on
# top of the existing CURSOR_VISIBLE_SECONDS pause.
_CURSOR_GLIDE_SECONDS = 0.35

# A real cursor-arrow silhouette (not a dot) as an inline SVG data URI —
# scaled well above a real OS cursor's on-screen size specifically because
# this renders inside a screenshot that then gets displayed inside a
# resizable, often-scaled-down "Live Stream" pane in the frontend
# (SiteViewer.jsx), not at 1:1 native resolution the way a real system
# cursor would. Blue (#2563eb) per explicit request, with a white outline
# so it stays legible against both light and dark page backgrounds.
_CURSOR_ARROW_SVG = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' width='36' height='36' viewBox='0 0 24 24'%3E"
    "%3Cpath d='M3 2 L3 20 L8 16 L11 22 L14 20.5 L11 14.5 L17 14.5 Z' "
    "fill='%232563eb' stroke='white' stroke-width='1.5' stroke-linejoin='round'/%3E"
    "%3C/svg%3E"
)

_CURSOR_OVERLAY_JS = """
([x, y, clicking]) => {
    let el = document.getElementById(%r);
    if (!el) {
        el = document.createElement("div");
        el.id = %r;
        el.innerHTML = `
            <div class="agentqa-cursor-arrow"></div>
            <div class="agentqa-cursor-ripple"></div>
        `;
        const style = document.createElement("style");
        style.textContent = `
            #%s { position: fixed; z-index: 2147483647; pointer-events: none;
                  width: 0; height: 0;
                  transition: left %fs ease-out, top %fs ease-out; }
            #%s .agentqa-cursor-arrow {
                /* The SVG's tip (the actual pointer point in the path,
                   roughly (3, 2) of a 24-unit viewBox scaled to 36px) sits
                   this far from the image's own top-left corner — offset
                   the element so the TIP, not the image's bounding box,
                   lands on the resolved target's coordinates. */
                position: absolute; left: -4px; top: -3px;
                width: 36px; height: 36px;
                background-image: url("%s");
                background-repeat: no-repeat;
                filter: drop-shadow(0 1px 3px rgba(0,0,0,0.5));
            }
            #%s .agentqa-cursor-ripple {
                position: absolute; left: 3px; top: 3px;
                width: 14px; height: 14px; border-radius: 50%%;
                border: 2px solid #2563eb; opacity: 0;
            }
            #%s.agentqa-cursor-clicking .agentqa-cursor-ripple {
                animation: agentqa-ripple 0.5s ease-out;
            }
            @keyframes agentqa-ripple {
                0%% { opacity: 0.8; transform: scale(1); }
                100%% { opacity: 0; transform: scale(2.8); }
            }
        `;
        document.head.appendChild(style);
        document.body.appendChild(el);
        // First placement ever (or right after a fresh page load, where
        // the previous element/position no longer exists): jump straight
        // there, nothing to glide from.
        el.style.left = x + "px";
        el.style.top = y + "px";
        el.classList.toggle("agentqa-cursor-clicking", !!clicking);
        return;
    }
    // requestAnimationFrame so the browser commits the element's CURRENT
    // position to the render tree before the transition's target changes
    // in the same tick — setting left/top directly back-to-back can
    // otherwise get coalesced into one paint with no visible transition
    // at all.
    requestAnimationFrame(() => {
        el.style.left = x + "px";
        el.style.top = y + "px";
        el.classList.toggle("agentqa-cursor-clicking", !!clicking);
    });
}
""" % (
    (_CURSOR_OVERLAY_ID, _CURSOR_OVERLAY_ID, _CURSOR_OVERLAY_ID, round(_CURSOR_GLIDE_SECONDS, 2), round(_CURSOR_GLIDE_SECONDS, 2))
    + (_CURSOR_OVERLAY_ID, _CURSOR_ARROW_SVG, _CURSOR_OVERLAY_ID, _CURSOR_OVERLAY_ID)
)

_CURSOR_OVERLAY_REMOVE_JS = """
() => {
    const el = document.getElementById(%r);
    if (el) el.remove();
}
""" % (_CURSOR_OVERLAY_ID,)


async def _show_cursor_at(page: Page, locator: Locator, clicking: bool = False) -> None:
    """Best-effort — a cursor overlay is a visual nicety for the live
    stream, never something that should break the actual action if the
    target moved off-screen or the page navigated away mid-call."""
    try:
        box = await locator.bounding_box(timeout=2000)
        if not box:
            return
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        await page.evaluate(_CURSOR_OVERLAY_JS, [x, y, clicking])
    except Exception:
        pass


async def _hide_cursor(page: Page) -> None:
    try:
        await page.evaluate(_CURSOR_OVERLAY_REMOVE_JS)
    except Exception:
        pass


async def publish_screenshot_frame(page: Page, event_bus: EventBus, run_id: str) -> None:
    """
    Pushed after every tool call (phase 5) so a connected WS client sees the
    browser update roughly in real time — not a live embedded DOM (see
    SiteViewer.jsx's comment on why iframe embedding is unworkable here),
    just a periodic frame. Cheap JPEG, in-memory base64 rather than a
    written file — this fires far more often than phase 2's
    screenshot-on-error-only pattern, so writing one file per frame would
    bloat the screenshots/ directory fast.

    Also called directly (not just from the tool loop) right after browser
    launch/navigation in agent/runner.py — without that, a viewer sees the
    stale "No test run in progress yet" placeholder for the entire
    Planner-thinking window (which can run 10-25+s) even though a run is
    genuinely active, since previously the very first frame only arrived
    after the first tool call completed.
    """
    try:
        png_or_jpeg = await page.screenshot(type="jpeg", quality=50, timeout=5000)
    except Exception:
        return  # best-effort — never let a failed screenshot break the caller
    data_url = "data:image/jpeg;base64," + base64.b64encode(png_or_jpeg).decode("ascii")
    event_bus.publish(run_id, "browser", "screenshot", {"image": data_url})


async def _publish_screenshot_frame(ctx: ToolContext) -> None:
    await publish_screenshot_frame(ctx.page, ctx.event_bus, ctx.run_id)


async def _record_step(
    ctx: ToolContext,
    *,
    step_type: StepType,
    intent: str,
    tool_name: str,
    tool_input: dict,
    status: str,
    started_at: datetime,
    selector_used: Optional[str] = None,
    selector_strategy: Optional[SelectorStrategy] = None,
    actual_result: Optional[str] = None,
    error_detail: Optional[str] = None,
    screenshot_url: Optional[str] = None,
) -> ExecutionStep:
    sensitive = tool_name == "fill" and is_sensitive_hint(str(tool_input.get("selector_hint", "")))
    step = ExecutionStep(
        id=uuid.uuid4().hex[:12],
        run_id=ctx.run_id,
        test_case_id=ctx.test_case_id,
        step_index=ctx._next_step_index(),
        step_type=step_type,
        intent=intent,
        tool_name=tool_name,
        tool_input=redact_tool_input(tool_name, tool_input),
        selector_used=selector_used,
        selector_strategy=selector_strategy,
        actual_result="[REDACTED]" if sensitive and actual_result else actual_result,
        status=TestRunStatus.passed if status == "OK" else TestRunStatus.error,
        screenshot_url=screenshot_url,
        started_at=started_at,
        finished_at=datetime.utcnow(),
        error_detail=error_detail,
    )
    ctx.store.add_execution_step(step)
    ctx.steps.append(step)
    await _publish_screenshot_frame(ctx)
    return step


class _SelectorGuess(BaseModel):
    role: str
    name: str
    found: bool


async def _llm_select(ctx: ToolContext, hint: str, action_type: str) -> tuple[Optional[Locator], Optional[str]]:
    """
    Last-resort selector tier: only reached when every mechanical tier in
    selector_resolver.py has failed. One extra local-model call with the
    current page snapshot — see docs/BUILD-PLAN.md "Self-healing selectors".
    """
    snapshot = await _page_snapshot(ctx.page)
    try:
        guess = await structured_chat(
            system=(
                "You are helping a browser automation tool find an element on a web page. "
                "Given an accessibility-tree snapshot and a natural-language description of an "
                "element, respond with the ARIA role and accessible name of the best-matching "
                "element, or set found=false if nothing plausible matches."
            ),
            user_message=f"Looking for: {hint} (to {action_type})\n\nPage snapshot:\n{snapshot}",
            output_model=_SelectorGuess,
        )
    except LocalLLMError:
        return None, None

    if not guess or not guess.found:
        return None, None

    locator = ctx.page.get_by_role(guess.role, name=guess.name, exact=False)
    try:
        if await locator.count() >= 1:
            return locator.first, f"llm role={guess.role}[name~={guess.name!r}]"
    except Exception:
        pass
    return None, None


async def _resolve_and_act(ctx: ToolContext, action_type: str, hint: str, value: Optional[str] = None) -> str:
    started_at = datetime.utcnow()
    locator, strategy, description = await resolve_selector(
        ctx.page, hint, action_type, ctx.test_case_id, ctx.selector_cache
    )

    if locator is None:
        ctx.log(f"No mechanical selector matched {hint!r} — asking the model to pick one...")
        locator, description = await _llm_select(ctx, hint, action_type)
        strategy = SelectorStrategy.llm_selected
        if locator is None:
            await _record_step(
                ctx,
                step_type=StepType.action,
                intent=f"{action_type}: {hint}",
                tool_name=action_type,
                tool_input={"selector_hint": hint, "value": value},
                status="FAILED",
                selector_strategy=strategy,
                error_detail=f"Could not resolve any element for hint {hint!r}",
                started_at=started_at,
            )
            return f"Could not find an element matching {hint!r} on the page."

    ctx.log(f"{action_type}({hint!r}) -> resolved via {strategy.value}: {description}")

    # Show a visible cursor hovering over the resolved target and publish a
    # frame with it before actually acting — otherwise a live viewer only
    # ever sees before/after states with no indication of *where* on the
    # page the agent is about to interact, since Playwright dispatches
    # events directly with no real mouse movement to observe. The pause
    # after publishing matters as much as the cursor itself: the action
    # that follows (click/fill) typically completes in well under a
    # second, so without this a human watching the live stream never
    # actually perceives the cursor frame — it's technically sent but
    # replaced by the "after" frame before a viewer's eye can register it.
    #
    # _show_cursor_at sets a CSS transition on the move, but a transition
    # alone is invisible on the live stream — page.screenshot() captures
    # one static instant, not video, so without capturing SEVERAL frames
    # DURING the glide a viewer would still only ever see the two static
    # endpoints and perceive an instant jump regardless of the CSS.
    # Sampling a handful of frames across _CURSOR_GLIDE_SECONDS while the
    # transition plays out in real browser time is what actually makes the
    # movement read as a glide instead of a teleport.
    await _show_cursor_at(ctx.page, locator, clicking=(action_type in ("click", "select_option")))
    glide_frames = 8
    for _ in range(glide_frames):
        await publish_screenshot_frame(ctx.page, ctx.event_bus, ctx.run_id)
        await asyncio.sleep(_CURSOR_GLIDE_SECONDS / glide_frames)
    await asyncio.sleep(max(0.0, CURSOR_VISIBLE_SECONDS - _CURSOR_GLIDE_SECONDS))

    try:
        if action_type == "click":
            url_before_click = ctx.page.url
            await locator.click(timeout=8000)
            # A click can trigger an async reaction (form submit, validation
            # message, navigation) that hasn't rendered yet the instant
            # Playwright's own click() resolves. Two waits, because one
            # alone isn't enough: network-idle can return while the clicked
            # element is still showing a disabled/loading state (e.g. a
            # submit button reading "Signing in..." — no network activity
            # left to wait on, but the result hasn't landed yet), and on at
            # least one real page the resulting message is a transient toast
            # that appears for only ~1-3s and then clears itself — so this
            # also waits for the clicked element to leave any loading state
            # before returning, instead of leaving that race to whatever the
            # model does with its own turn-taking time afterward.
            try:
                await ctx.page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            try:
                await ctx.page.wait_for_function(
                    """(el) => {
                        if (!el) return true;
                        const label = (el.innerText || "").toLowerCase();
                        const loading = label.includes("loading") || label.includes("…")
                            || label.includes("...") || /\\bing\\.\\.\\.$/.test(label.trim());
                        return !(el.disabled || loading);
                    }""",
                    arg=await locator.element_handle(),
                    timeout=4000,
                )
            except Exception:
                pass
            # Neither wait above covers a click that causes a full
            # navigation (e.g. Sign In triggering a cross-domain OAuth
            # redirect back to the app): "the clicked element's loading
            # state cleared" and even "networkidle" can both be true on the
            # DESTINATION page well before that page's own client-side data
            # fetches (e.g. dashboard summary cards) have actually rendered
            # — confirmed live, ~1s gap measured between the URL settling
            # and the real content appearing. locator.element_handle() from
            # before the navigation is also now stale/detached, so the
            # loading-state wait above silently no-ops on a fresh page
            # rather than actually checking anything useful. Detected via a
            # plain URL comparison (not a Playwright navigation-promise
            # race, since the navigation already happened by this point) —
            # only pay this extra wait when a real navigation is why it's
            # needed, not on every ordinary same-page click.
            if ctx.page.url != url_before_click:
                try:
                    await ctx.page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
            # Confirmed live (see test_cursor_glide.py /
            # test_cursor_overlay_js_template_renders_without_a_formatting_error's
            # neighboring coverage) that the overlay — a plain <div> with no
            # ARIA role — never appears in aria_snapshot() output, so it no
            # longer needs to be hidden before this read. Left visible
            # (moved, not hidden+reshown) between actions now, on purpose:
            # a live viewer previously saw the cursor vanish after every
            # single action and only reappear right before the next one,
            # which read as flickering rather than a continuously present
            # pointer — see _show_cursor_at's glide, which now animates
            # FROM wherever it's still sitting TO the next target instead
            # of popping in from nothing each time.
            #
            # Capture the page state right here, immediately after the
            # click settles, into post_action_snapshot — deliberately kept
            # separate from last_snapshot (see ToolContext) so a later,
            # staler assert_condition/read_page call can't silently
            # overwrite this earlier, more relevant evidence.
            ctx.post_action_snapshot = await _page_snapshot(ctx.page)
            result = f"Clicked element matching {hint!r}"
        elif action_type == "fill":
            # value may be a literal {{VALID_EMAIL}}/{{VALID_PASSWORD}} token
            # (see agent/tools/redaction.py) — resolved to the real secret
            # only for this call; every other reference (result text, log,
            # trace) keeps the token itself, never the real value.
            real_value = resolve_credential_placeholder(value)
            await locator.fill(real_value or "", timeout=8000)
            result = f"Filled {hint!r} with {value!r}"
        elif action_type == "select_option":
            # Dropdowns on this site are custom comboboxes, not native
            # <select> elements — open, then click the option by visible text.
            await locator.click(timeout=8000)
            option = ctx.page.get_by_role("option", name=value, exact=True)
            await option.click(timeout=8000)
            result = f"Selected {value!r} in {hint!r}"
        else:
            result = f"Unknown action type {action_type!r}"

        await _record_step(
            ctx,
            step_type=StepType.action,
            intent=f"{action_type}: {hint}",
            tool_name=action_type,
            tool_input={"selector_hint": hint, "value": value},
            status="OK",
            selector_used=description,
            selector_strategy=strategy,
            actual_result=result,
            started_at=started_at,
        )
        return result
    except Exception as exc:
        await _hide_cursor(ctx.page)
        await _record_step(
            ctx,
            step_type=StepType.action,
            intent=f"{action_type}: {hint}",
            tool_name=action_type,
            tool_input={"selector_hint": hint, "value": value},
            status="FAILED",
            selector_used=description,
            selector_strategy=strategy,
            error_detail=str(exc),
            started_at=started_at,
        )
        return f"Action failed: {exc}"


def build_tools(ctx: ToolContext) -> list:
    """Returns the six @local_tool-decorated closures bound to ctx."""

    @local_tool
    async def click(selector_hint: str) -> str:
        """Click an element on the page. selector_hint should describe the element in plain
        language, e.g. "the Save button" or "the Sign In link"."""
        return await _resolve_and_act(ctx, "click", selector_hint)

    @local_tool
    async def fill(selector_hint: str, value: str) -> str:
        """Fill a text input or textarea. selector_hint describes the field, e.g. "the Line Item
        Name field"."""
        return await _resolve_and_act(ctx, "fill", selector_hint, value=value)

    @local_tool
    async def select_option(selector_hint: str, value: str) -> str:
        """Open a dropdown/combobox and pick an option. selector_hint describes the dropdown,
        value is the visible option text to select."""
        return await _resolve_and_act(ctx, "select_option", selector_hint, value=value)

    @local_tool
    async def navigate(url: str) -> str:
        """Navigate the browser to a URL."""
        started_at = datetime.utcnow()
        ctx.log(f"Navigating to {url}...")
        try:
            await ctx.page.goto(url, wait_until="networkidle", timeout=20000)
            await _record_step(
                ctx,
                step_type=StepType.navigate,
                intent=f"Navigate to {url}",
                tool_name="navigate",
                tool_input={"url": url},
                status="OK",
                actual_result=f"Now at {ctx.page.url}",
                started_at=started_at,
            )
            return f"Navigated to {ctx.page.url}"
        except Exception as exc:
            await _record_step(
                ctx,
                step_type=StepType.navigate,
                intent=f"Navigate to {url}",
                tool_name="navigate",
                tool_input={"url": url},
                status="FAILED",
                error_detail=str(exc),
                started_at=started_at,
            )
            return f"Navigation failed: {exc}"

    @local_tool
    async def read_page(scope_hint: str = "") -> str:
        """Read a trimmed snapshot of the current page's visible text and interactive elements —
        use this to see what's on screen before deciding the next action. scope_hint is currently
        advisory only (the full page is always returned)."""
        started_at = datetime.utcnow()
        snapshot = await _page_snapshot(ctx.page)
        ctx.last_snapshot = snapshot
        await _record_step(
            ctx,
            step_type=StepType.observation,
            intent=f"Read page ({scope_hint or 'full page'})",
            tool_name="read_page",
            tool_input={"scope_hint": scope_hint},
            status="OK",
            actual_result=snapshot[:500],
            started_at=started_at,
        )
        return snapshot

    @local_tool
    async def assert_condition(description: str) -> str:
        """Assert something about the current page state, e.g. "a validation error is shown for
        the Rate field" or "the page navigated away from the form". The tool result is the
        current page's state — you judge pass/fail against `description` on your next turn; this
        tool does not decide it for you."""
        started_at = datetime.utcnow()
        snapshot = await _page_snapshot(ctx.page)
        ctx.last_snapshot = snapshot
        await _record_step(
            ctx,
            step_type=StepType.assertion,
            intent=description,
            tool_name="assert_condition",
            tool_input={"description": description},
            status="OK",
            actual_result=snapshot[:500],
            started_at=started_at,
        )
        return f"Current page state, for judging {description!r}:\n{snapshot}"

    return [click, fill, select_option, navigate, read_page, assert_condition]
