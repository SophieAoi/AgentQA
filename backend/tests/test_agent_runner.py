from unittest.mock import AsyncMock, patch

from app.models.schemas import TestRunStatus, TestStepResult
from app.services.agent_runner import run_test_suite
from app.services.event_bus import EventBus
from app.services.store import InMemoryStore


async def test_run_test_case_fails_cleanly_with_no_credentials_configured(store: InMemoryStore):
    """
    No network access needed: run_test_case() should launch a real browser,
    hit login()'s missing-credentials check immediately, capture a
    screenshot, and return a single FAILED step — never raise.
    """
    from agent.runner import run_test_case

    with patch("agent.browser.login.INFLUENCE_TEST_USERNAME", None), patch(
        "agent.browser.login.INFLUENCE_TEST_PASSWORD", None
    ):
        run = store.create_test_run(["TC-001"])
        steps = await run_test_case(store, EventBus(), run.run_id, "TC-001")

    assert len(steps) == 1
    assert steps[0].status == "FAILED"
    assert "not set" in steps[0].detail
    assert steps[0].screenshot_url.startswith("/screenshots/")


async def test_run_test_suite_reaches_failed_status_when_test_case_fails(store: InMemoryStore):
    run = store.create_test_run(["TC-001"])

    async def fake_run_test_case(store_arg, event_bus_arg, run_id, test_case_id, on_step=None, session=None):
        step = TestStepResult(step_description="Log in", status="FAILED", detail="no creds")
        if on_step:
            on_step(step)
        return [step]

    with patch("app.services.agent_runner.run_test_case", fake_run_test_case):
        await run_test_suite(store, EventBus(), run.run_id, ["TC-001"])

    updated = store.get_test_run(run.run_id)
    assert updated.status == TestRunStatus.failed
    assert updated.failed_count == 1
    assert updated.passed_count == 0
    assert updated.finished_at is not None


async def test_run_test_suite_reaches_passed_status_when_all_steps_ok(store: InMemoryStore):
    run = store.create_test_run(["TC-001"])

    async def fake_run_test_case(store_arg, event_bus_arg, run_id, test_case_id, on_step=None, session=None):
        step = TestStepResult(step_description="Log in", status="OK")
        if on_step:
            on_step(step)
        return [step]

    with patch("app.services.agent_runner.run_test_case", fake_run_test_case):
        await run_test_suite(store, EventBus(), run.run_id, ["TC-001"])

    updated = store.get_test_run(run.run_id)
    assert updated.status == TestRunStatus.passed
    assert updated.passed_count == 1


async def test_run_test_suite_tracks_current_case_and_per_case_results(store: InMemoryStore):
    """
    Regression test: a multi-case run should expose which case is running
    (current_test_case_id/index) and a per-case scoreboard (case_results),
    not just an aggregate pass/fail count — the frontend needs both to show
    real progress instead of a single "N passed" number that only updates
    at the very end of each case.
    """
    run = store.create_test_run(["TC-001", "TC-002"])
    seen_current_during_run = []

    async def fake_run_test_case(store_arg, event_bus_arg, run_id, test_case_id, on_step=None, session=None):
        seen_current_during_run.append(store_arg.get_test_run(run_id).current_test_case_id)
        status = "OK" if test_case_id == "TC-001" else "FAILED"
        step = TestStepResult(step_description="Step", status=status)
        if on_step:
            on_step(step)
        return [step]

    with patch("app.services.agent_runner.run_test_case", fake_run_test_case):
        await run_test_suite(store, EventBus(), run.run_id, ["TC-001", "TC-002"])

    assert seen_current_during_run == ["TC-001", "TC-002"]

    updated = store.get_test_run(run.run_id)
    assert updated.current_test_case_id is None  # cleared once the run finishes
    assert updated.current_test_case_index is None
    assert [r.test_case_id for r in updated.case_results] == ["TC-001", "TC-002"]
    assert [r.status for r in updated.case_results] == ["passed", "failed"]


async def test_run_test_suite_reuses_one_browser_session_across_consecutive_login_cases(
    store: InMemoryStore,
):
    """
    Cost-reduction regression test: a real browser launch+navigate
    measured ~1.6-2.3s live. TC-001 and TC-002 both require login (see
    their YAML — neither is a login-suite case), so a batch running them
    back-to-back should share ONE BrowserSession instead of paying that
    cost twice — the same `session` object must be passed to run_test_case
    for both.
    """
    run = store.create_test_run(["TC-001", "TC-002"])
    seen_sessions = []

    async def fake_run_test_case(store_arg, event_bus_arg, run_id, test_case_id, on_step=None, session=None):
        seen_sessions.append(session)
        step = TestStepResult(step_description="Step", status="OK")
        if on_step:
            on_step(step)
        return [step]

    fake_session_instance = AsyncMock()
    fake_session_instance.__aenter__ = AsyncMock(return_value=fake_session_instance)

    with patch("app.services.agent_runner.run_test_case", fake_run_test_case), patch(
        "app.services.agent_runner.BrowserSession", return_value=fake_session_instance
    ):
        await run_test_suite(store, EventBus(), run.run_id, ["TC-001", "TC-002"])

    assert len(seen_sessions) == 2
    assert seen_sessions[0] is seen_sessions[1]
    assert seen_sessions[0] is fake_session_instance
    fake_session_instance.close.assert_awaited_once()  # closed once at the end, not per case


async def test_run_test_suite_opens_a_fresh_session_for_a_login_suite_case(store: InMemoryStore):
    """
    AD_LG_01 is a real login-suite case (no "requires login" precondition —
    it specifically tests the logged-out flow). A batch mixing it with a
    login-requiring case must NOT hand AD_LG_01 a session that might
    already be authenticated from an earlier case — run_test_case must be
    called with session=None for it, and any previously-shared session
    must be closed first so the next login-requiring case gets a new one
    rather than silently reusing a torn-down object.
    """
    run = store.create_test_run(["TC-001", "AD_LG_01", "TC-002"])
    seen_sessions = []

    async def fake_run_test_case(store_arg, event_bus_arg, run_id, test_case_id, on_step=None, session=None):
        seen_sessions.append((test_case_id, session))
        step = TestStepResult(step_description="Step", status="OK")
        if on_step:
            on_step(step)
        return [step]

    made_sessions = []

    def _make_fake_session():
        s = AsyncMock()
        s.__aenter__ = AsyncMock(return_value=s)
        made_sessions.append(s)
        return s

    with patch("app.services.agent_runner.run_test_case", fake_run_test_case), patch(
        "app.services.agent_runner.BrowserSession", side_effect=_make_fake_session
    ):
        await run_test_suite(store, EventBus(), run.run_id, ["TC-001", "AD_LG_01", "TC-002"])

    ids_and_sessions = dict(seen_sessions)
    assert ids_and_sessions["AD_LG_01"] is None
    # TC-001 got a real (fresh) shared session; TC-002 (after AD_LG_01) got
    # a DIFFERENT fresh session, not the one TC-001 used and AD_LG_01 forced closed.
    assert ids_and_sessions["TC-001"] is not None
    assert ids_and_sessions["TC-002"] is not None
    assert ids_and_sessions["TC-001"] is not ids_and_sessions["TC-002"]
    # The session used for TC-001 was closed before AD_LG_01 ran (not left
    # dangling), and the one made for TC-002 is closed at the very end.
    assert len(made_sessions) == 2
    for s in made_sessions:
        s.close.assert_awaited_once()
