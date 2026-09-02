"""
Regression coverage for the cursor-glide frames added to
agent/tools/playwright_tools.py::_resolve_and_act() — the cursor overlay
previously teleported instantly between positions (no CSS transition) and
only ever published ONE screenshot frame per action, so a live viewer only
ever saw the two static endpoints and perceived an instant jump regardless
of any transition, since page.screenshot() captures a single static
instant, not video. Sampling several frames DURING a short animated
transition is what actually makes the movement read as a glide.
"""

from pathlib import Path

from agent.browser.session import BrowserSession
from agent.tools.playwright_tools import ToolContext, build_tools
from agent.tools.selector_resolver import SelectorCache
from app.services.event_bus import EventBus
from app.services.store import InMemoryStore

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
INTERACTIVE_PAGE = f"file://{FIXTURES_DIR / 'interactive_page.html'}"


async def test_click_publishes_multiple_screenshot_frames_during_the_glide():
    async with BrowserSession(headless=True) as session:
        await session.goto(INTERACTIVE_PAGE)

        event_bus = EventBus()
        queue = event_bus.subscribe("run-1", "browser")
        ctx = ToolContext(
            page=session.page,
            store=InMemoryStore(),
            run_id="run-1",
            test_case_id="TC-TEST",
            selector_cache=SelectorCache(),
            log=lambda msg: None,
            event_bus=event_bus,
        )
        tools = {t.name: t for t in build_tools(ctx)}

        await tools["click"](selector_hint="Sign In")

        frames = []
        while not queue.empty():
            frames.append(queue.get_nowait())

        # More than the single before-action frame the old teleport
        # implementation published — several frames sampled across the
        # glide window, not just one static endpoint.
        assert len(frames) >= 4
        assert all(f.type == "screenshot" for f in frames)


async def test_cursor_stays_visible_after_a_successful_click():
    """
    Regression test: previously _hide_cursor() ran right after every
    successful click/fill/select_option, so a live viewer saw the cursor
    vanish after every single action and only reappear right before the
    next one — flickering rather than a continuously present pointer.
    _hide_cursor() is now only called on the error path; a successful
    action leaves the overlay visible at its resting position so the next
    action's glide animates FROM there instead of popping in from nothing.
    """
    async with BrowserSession(headless=True) as session:
        await session.goto(INTERACTIVE_PAGE)

        event_bus = EventBus()
        ctx = ToolContext(
            page=session.page,
            store=InMemoryStore(),
            run_id="run-1",
            test_case_id="TC-TEST",
            selector_cache=SelectorCache(),
            log=lambda msg: None,
            event_bus=event_bus,
        )
        tools = {t.name: t for t in build_tools(ctx)}

        await tools["click"](selector_hint="Sign In")

        from agent.tools.playwright_tools import _CURSOR_OVERLAY_ID

        still_present = await session.page.evaluate(
            "(id) => !!document.getElementById(id)", _CURSOR_OVERLAY_ID
        )
        assert still_present


async def test_cursor_overlay_js_template_renders_without_a_formatting_error():
    """
    _CURSOR_OVERLAY_JS is built via old-style %-formatting with several
    positional placeholders (the overlay id repeated, plus the glide
    duration twice) — a mismatched placeholder count silently raises a
    TypeError at import time. Import succeeding is itself most of this
    test; asserting the rendered id string is still a real regression
    guard against a future edit changing the placeholder count without
    updating the substitution tuple.
    """
    from agent.tools.playwright_tools import _CURSOR_OVERLAY_ID, _CURSOR_OVERLAY_JS

    assert _CURSOR_OVERLAY_ID in _CURSOR_OVERLAY_JS
    assert "transition: left" in _CURSOR_OVERLAY_JS
    assert "agentqa-cursor-arrow" in _CURSOR_OVERLAY_JS
    assert "%232563eb" in _CURSOR_OVERLAY_JS  # blue, URL-encoded in the inline SVG's fill
