"""
This is the bridge between the API and the real agent code (agent/runner.py,
agent/browser/). Phase 2 (docs/phase-02-playwright-skeleton.md) replaced the
simulated per-test-case work here with real Playwright execution. Phase 5
(docs/phase-05-live-streaming-websockets.md) added event_bus publishing
alongside every store write, so a connected WebSocket client sees the same
progress in real time instead of only via polling.
"""

from datetime import datetime
from typing import Optional

from app.models.schemas import TestCaseRunResult, TestRunStatus, TestStepResult
from app.services.event_bus import EventBus
from app.services.store import StoreProtocol
from agent.browser.session import BrowserSession
from agent.runner import run_test_case, test_case_requires_fresh_session


async def run_test_suite(
    store: StoreProtocol, event_bus: EventBus, run_id: str, test_case_ids: list[str]
) -> None:
    """
    Runs a whole suite of test cases for one run, updating the shared
    store as it goes so the frontend can poll for live progress —
    both the per-step pass/fail results and a line-by-line action log.

    Any unhandled exception here previously left the run stuck at
    status=running forever with no client-visible failure (audit finding
    F-005) — the whole body is wrapped so a crash always reaches a
    terminal status.
    """
    try:
        await _run_test_suite_body(store, event_bus, run_id, test_case_ids)
    except Exception as exc:
        message = f"Run failed with an unexpected error: {exc}"
        store.add_log(run_id, message)
        event_bus.publish(run_id, "logs", "log", {"message": message})
        store.update_test_run(run_id, status=TestRunStatus.error, finished_at=datetime.utcnow())


async def _run_test_suite_body(
    store: StoreProtocol, event_bus: EventBus, run_id: str, test_case_ids: list[str]
) -> None:
    store.update_test_run(run_id, status=TestRunStatus.running)

    steps: list[TestStepResult] = []
    passed_count = 0
    failed_count = 0
    # One entry per test case, in queue order, so the frontend can render a
    # running scoreboard ("3 passed, 1 failed, 11 pending") instead of only
    # the aggregate pass/fail counts, which say nothing about which case is
    # currently running or what's still queued.
    case_results = [
        TestCaseRunResult(test_case_id=tc_id, status="pending") for tc_id in test_case_ids
    ]

    def log(message: str) -> None:
        store.add_log(run_id, message)
        event_bus.publish(run_id, "logs", "log", {"message": message})

    def on_step(step: TestStepResult) -> None:
        # Pushed to the store — and now the event bus — as each individual
        # step completes, not just at test-case boundaries.
        steps.append(step)
        store.update_test_run(run_id, steps=list(steps))
        event_bus.publish(run_id, "logs", "step", step.model_dump(mode="json"))

    # A fresh Playwright browser launch+navigate measured ~1.6-2.3s live —
    # real, avoidable overhead when a batch has several consecutive cases
    # that all just need to already be logged in. Reused across such a
    # run, closed and reopened fresh only when a case that specifically
    # needs an unauthenticated session (the login suite —
    # test_case_requires_fresh_session()) comes up, since reusing an
    # already-authenticated session there would silently break the very
    # thing those cases test (agent/runner.py's is_logged_in() handles the
    # inverse case — a reused session that's ALREADY logged in from an
    # earlier case not re-running login()'s fill/click flow, which would
    # fail outright against an already-logged-in dashboard).
    shared_session: Optional[BrowserSession] = None

    try:
        for index, test_case_id in enumerate(test_case_ids):
            log(f"Starting {test_case_id}...")
            case_results[index].status = "running"
            store.update_test_run(
                run_id,
                current_test_case_id=test_case_id,
                current_test_case_index=index + 1,
                case_results=list(case_results),
            )
            event_bus.publish(
                run_id,
                "logs",
                "case_started",
                {"test_case_id": test_case_id, "index": index + 1, "total": len(test_case_ids)},
            )

            if test_case_requires_fresh_session(test_case_id):
                if shared_session is not None:
                    await shared_session.close()
                    shared_session = None
                case_steps = await run_test_case(store, event_bus, run_id, test_case_id, on_step=on_step)
            else:
                if shared_session is None:
                    log("Launching browser...")
                    shared_session = BrowserSession()
                    await shared_session.__aenter__()
                case_steps = await run_test_case(
                    store, event_bus, run_id, test_case_id, on_step=on_step, session=shared_session
                )

            if case_steps and all(step.status == "OK" for step in case_steps):
                passed_count += 1
                case_results[index].status = "passed"
                log(f"✓ {test_case_id} passed")
            else:
                failed_count += 1
                case_results[index].status = "failed"
                log(f"✗ {test_case_id} failed")

            store.update_test_run(
                run_id,
                steps=list(steps),
                passed_count=passed_count,
                failed_count=failed_count,
                case_results=list(case_results),
            )
    finally:
        if shared_session is not None:
            await shared_session.close()

    final_status = TestRunStatus.passed if failed_count == 0 else TestRunStatus.failed
    store.update_test_run(
        run_id,
        status=final_status,
        finished_at=datetime.utcnow(),
        current_test_case_id=None,
        current_test_case_index=None,
    )
    event_bus.publish(run_id, "logs", "run_finished", {"status": final_status.value})
