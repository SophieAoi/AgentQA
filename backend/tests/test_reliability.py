"""
Phase 4 reliability tests: retry-with-backoff on a transient executor
failure, run-level wall-clock timeout so a runaway agent loop can't hang a
run forever, and the verifier being consulted (instead of raw RESULT:
PASS/FAILED text-parsing) when the executor captured real DOM state.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from agent.executor import run_executor
from agent.local_llm import LocalLLMError, ToolLoopTurn
from agent.verifier import VerifierError
from app.models.schemas import PlannedStep, StepType, VerificationResult
from app.services.event_bus import EventBus
from app.services.store import InMemoryStore


def _tool_call_turn(tool_name: str = "click", tool_input: dict | None = None, tool_result: str = "Clicked"):
    return ToolLoopTurn(
        type="tool_call", tool_name=tool_name, tool_input=tool_input or {}, tool_result=tool_result
    )


def _text_turn(text: str):
    return ToolLoopTurn(type="text", text=text, tokens_in=10, tokens_out=20)


def _fake_run_tool_loop(turns_to_yield):
    async def fake(**kwargs):
        for turn in turns_to_yield:
            yield turn

    return fake


def _tool_calling_fake(tool_name: str, tool_input: dict, final_text="Looks right.\nRESULT: PASS"):
    """
    Unlike _fake_run_tool_loop (which just yields pre-scripted turns), this
    double actually invokes the real tool function matching tool_name —
    needed to exercise assert_condition's real side effect (setting
    ctx.last_snapshot) the way the real local-model tool loop would.
    """

    async def fake(**kwargs):
        tools_by_name = {t.name: t for t in kwargs.get("tools", [])}
        tool = tools_by_name[tool_name]
        result = await tool(**tool_input)
        yield ToolLoopTurn(type="tool_call", tool_name=tool_name, tool_input=tool_input, tool_result=result)
        yield ToolLoopTurn(type="text", text=final_text, tokens_in=5, tokens_out=5)

    return fake


async def test_transient_failure_retries_then_succeeds():
    """Fails twice, succeeds on the 3rd attempt -> final status OK, no
    retries left over (MAX_STEP_RETRIES=2 means 3 total attempts)."""
    steps = [PlannedStep(step_index=1, intent="Click something", step_type=StepType.action)]

    attempts = {"count": 0}

    def factory(**kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise LocalLLMError("simulated transient local-model connection failure")
        return _fake_run_tool_loop([_tool_call_turn(), _text_turn("Clicked it.")])(**kwargs)

    with patch("agent.executor.run_tool_loop", factory), patch(
        "agent.executor.asyncio.sleep", new=AsyncMock()
    ) as mock_sleep:
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert attempts["count"] == 3
    assert len(results) == 1
    assert results[0].status == "OK"
    assert mock_sleep.await_count == 2  # backed off before attempts 2 and 3


async def test_exhausted_retries_marks_step_as_error_not_failed():
    """A tool-loop exception that never resolves is an infra problem
    (ERROR), not a judged-false assertion (FAILED)."""
    steps = [PlannedStep(step_index=1, intent="Click something", step_type=StepType.action)]

    def factory(**kwargs):
        raise LocalLLMError("local model unreachable")

    with patch("agent.executor.run_tool_loop", factory), patch(
        "agent.executor.asyncio.sleep", new=AsyncMock()
    ):
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert results[0].status == "ERROR"
    assert "3 attempt(s)" in results[0].detail


async def test_run_test_case_times_out_when_body_hangs():
    """A run_test_case body that never resolves reaches status=ERROR within
    the configured timeout window, instead of hanging the run forever."""
    import asyncio

    from agent.runner import run_test_case

    async def hang_forever(*args, **kwargs):
        await asyncio.sleep(10)

    with patch("agent.runner.TEST_CASE_TIMEOUT_SECONDS", 0.05), patch(
        "agent.runner._run_test_case_body", hang_forever
    ):
        steps = await run_test_case(InMemoryStore(), EventBus(), "run1", "TC-001")

    assert len(steps) == 1
    assert steps[0].status == "ERROR"
    assert "timeout" in steps[0].detail.lower()


async def test_run_test_case_timeout_invokes_on_step_callback():
    import asyncio

    from agent.runner import run_test_case

    async def hang_forever(*args, **kwargs):
        await asyncio.sleep(10)

    seen = []

    with patch("agent.runner.TEST_CASE_TIMEOUT_SECONDS", 0.05), patch(
        "agent.runner._run_test_case_body", hang_forever
    ):
        await run_test_case(InMemoryStore(), EventBus(), "run1", "TC-001", on_step=seen.append)

    assert len(seen) == 1
    assert seen[0].status == "ERROR"


async def test_assertion_step_uses_verifier_when_dom_state_captured():
    """When the executor actually read page state (via assert_condition),
    the verifier's structured judgment drives pass/fail and confidence —
    not the executor's own RESULT: PASS/FAILED text."""
    steps = [
        PlannedStep(
            step_index=1,
            intent="Assert the dashboard is shown",
            step_type=StepType.assertion,
            expected_outcome="Dashboard is visible",
        )
    ]

    verifier_result = VerificationResult(status="failed", confidence=0.9, explanation="No dashboard heading.")

    store = InMemoryStore()
    page = MagicMock()
    page.locator.return_value.aria_snapshot = AsyncMock(return_value="heading: Login")

    with patch(
        "agent.executor.run_tool_loop",
        _tool_calling_fake("assert_condition", {"description": "check dashboard"}),
    ), patch("agent.executor.verify", new=AsyncMock(return_value=verifier_result)) as mock_verify:
        results = await run_executor(page, store, EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None)

    # Even though the executor's own text said RESULT: PASS, the verifier's
    # judgment (status=failed) is what actually decides the outcome.
    assert results[0].status == "FAILED"
    assert mock_verify.await_args.args[0] == "Dashboard is visible"
    assert "heading: Login" in mock_verify.await_args.args[1]


async def test_verifier_error_marks_step_as_error():
    steps = [
        PlannedStep(
            step_index=1,
            intent="Assert the dashboard is shown",
            step_type=StepType.assertion,
            expected_outcome="Dashboard is visible",
        )
    ]

    store = InMemoryStore()
    page = MagicMock()
    page.locator.return_value.aria_snapshot = AsyncMock(return_value="heading: Login")

    with patch(
        "agent.executor.run_tool_loop",
        _tool_calling_fake("assert_condition", {"description": "check dashboard"}),
    ), patch("agent.executor.verify", new=AsyncMock(side_effect=VerifierError("local model unreachable"))):
        results = await run_executor(page, store, EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None)

    assert results[0].status == "ERROR"
    assert "Verifier error" in results[0].detail
