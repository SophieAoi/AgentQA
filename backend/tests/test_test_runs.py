from unittest.mock import patch

from app.models.schemas import TestRunStatus, TestStepResult
from app.services.agent_runner import run_test_suite
from app.services.event_bus import EventBus


def test_start_test_run_rejects_empty_test_case_ids(client):
    response = client.post("/test-runs", json={"test_case_ids": []})
    assert response.status_code == 400


def test_get_test_run_404s_for_unknown_id(client):
    response = client.get("/test-runs/does-not-exist")
    assert response.status_code == 404


def test_start_test_run_creates_a_queued_run_and_lists_it(client):
    """
    This only exercises the REST contract (a run is created and appears in
    the listing) — the actual agent run is mocked out. Starlette's
    TestClient runs BackgroundTasks synchronously as part of handling the
    request, so an unmocked run_test_case would make this test actually
    launch a real browser and hit the real local model.
    """

    async def fake_run_test_case(store_arg, event_bus_arg, run_id, test_case_id, on_step=None, session=None):
        return []

    with patch("app.services.agent_runner.run_test_case", fake_run_test_case):
        response = client.post("/test-runs", json={"test_case_ids": ["TC-001"]})
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    listing = client.get("/test-runs")
    assert any(r["run_id"] == run_id for r in listing.json())


async def test_run_test_suite_marks_run_as_error_on_unhandled_exception(store):
    """
    Regression test for audit finding F-005: an unhandled exception inside
    the per-test-case loop used to leave the run stuck at status=running
    forever. run_test_suite now wraps the body in try/except and always
    reaches a terminal status.
    """
    run = store.create_test_run(["TC-001"])

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated crash")

    import app.services.agent_runner as agent_runner_module

    original = agent_runner_module._run_test_suite_body
    agent_runner_module._run_test_suite_body = boom
    try:
        await run_test_suite(store, EventBus(), run.run_id, ["TC-001"])
    finally:
        agent_runner_module._run_test_suite_body = original

    updated = store.get_test_run(run.run_id)
    assert updated.status == TestRunStatus.error
    assert updated.finished_at is not None


async def test_run_test_suite_reaches_a_terminal_status(store):
    """
    Uses a mocked run_test_case — this suite must never depend on the real
    staging site's availability, credentials, or current inventory data
    (see docs/phase-02-playwright-skeleton.md). Real end-to-end coverage
    against the live site is a manual verification step, not part of the
    default test run.
    """
    run = store.create_test_run(["TC-001"])

    async def fake_run_test_case(store_arg, event_bus_arg, run_id, test_case_id, on_step=None, session=None):
        step = TestStepResult(step_description="Log in", status="OK")
        if on_step:
            on_step(step)
        return [step]

    with patch("app.services.agent_runner.run_test_case", fake_run_test_case):
        await run_test_suite(store, EventBus(), run.run_id, ["TC-001"])

    updated = store.get_test_run(run.run_id)
    assert updated.status in (TestRunStatus.passed, TestRunStatus.failed)
    assert updated.finished_at is not None
