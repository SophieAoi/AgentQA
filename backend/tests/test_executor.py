from unittest.mock import AsyncMock, MagicMock, patch

from agent.executor import run_executor
from agent.local_llm import ToolLoopTurn
from app.models.schemas import PlannedStep, StepType, VerificationResult
from app.services.event_bus import EventBus
from app.services.store import InMemoryStore


def _text_turn(text: str):
    return ToolLoopTurn(type="text", text=text, tokens_in=10, tokens_out=20)


def _tool_call_turn(tool_name: str = "click", tool_input: dict | None = None, tool_result: str = "Clicked"):
    return ToolLoopTurn(
        type="tool_call", tool_name=tool_name, tool_input=tool_input or {}, tool_result=tool_result
    )


def _fake_run_tool_loop(turns_to_yield):
    """
    Minimal async-generator test double for agent.local_llm.run_tool_loop().
    Yields pre-scripted ToolLoopTurns; ignores the real messages/tools kwargs
    since these tests only care about executor.py's response to each turn.
    """

    async def fake(**kwargs):
        for turn in turns_to_yield:
            yield turn

    return fake


async def test_run_executor_action_step_passes_when_a_real_tool_call_happened():
    steps = [PlannedStep(step_index=1, intent="Click the Sign In button", step_type=StepType.action)]

    with patch(
        "agent.executor.run_tool_loop",
        _fake_run_tool_loop([_tool_call_turn(), _text_turn("Clicked the button.")]),
    ):
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert len(results) == 1
    assert results[0].status == "OK"


async def test_run_executor_skips_the_narration_call_for_a_plain_successful_action():
    """
    Cost-reduction regression test: a plain action step (no expected_outcome
    to verify) whose tool call is a dedupable tool (click/fill/select_option
    /navigate) and unambiguously succeeded doesn't need a second real LLM
    call just for the model to restate "RESULT: PASS" — that verdict is
    already fully determined by the tool's own return value. executor.py
    should break out of the async-for loop right after the successful
    tool_call turn, never consuming/tracing a text turn that comes after it
    in the (test-double) generator — proving the second call was genuinely
    never awaited, not just that the final status happens to come out right.
    """
    steps = [PlannedStep(step_index=1, intent="Click the Sign In button", step_type=StepType.action)]
    traced_after_tool_call = []

    async def fake_run_tool_loop(**kwargs):
        yield _tool_call_turn(tool_name="click", tool_result="Clicked element matching 'Sign In'")
        # If executor.py keeps iterating past the tool_call turn instead of
        # breaking, this text turn gets consumed and traced — the assertion
        # below checks it never was.
        traced_after_tool_call.append("reached")
        yield _text_turn("Clicked the Sign In button.\nRESULT: PASS")

    with patch("agent.executor.run_tool_loop", fake_run_tool_loop):
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert len(results) == 1
    assert results[0].status == "OK"
    assert results[0].detail is None
    assert traced_after_tool_call == []  # the generator was abandoned before this ran


async def test_run_executor_does_not_skip_narration_when_the_tool_call_fails():
    """The short-circuit must not fire when the tool's own result string is
    the known failure marker — the model still needs to weigh in via
    RESULT: PASS/FAILED for a step whose action didn't cleanly succeed."""
    steps = [PlannedStep(step_index=1, intent="Click the Sign In button", step_type=StepType.action)]

    with patch(
        "agent.executor.run_tool_loop",
        _fake_run_tool_loop(
            [
                _tool_call_turn(tool_result="Could not find an element matching 'Sign In'."),
                _text_turn("The button could not be found.\nRESULT: FAILED"),
            ]
        ),
    ):
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert results[0].status == "FAILED"
    assert "could not be found" in results[0].detail.lower()


async def test_run_executor_does_not_skip_narration_when_the_step_needs_verification():
    """A step with an expected_outcome still needs the model's own text
    turn — the short-circuit only applies to plain action steps with
    nothing to verify."""
    steps = [
        PlannedStep(
            step_index=1,
            intent="Click Sign In",
            step_type=StepType.action,
            expected_outcome="An error message is shown",
        )
    ]
    verdict = VerificationResult(status="passed", confidence=0.9, explanation="Shown.")

    called = {"n": 0}

    async def fake_run_tool_loop(**kwargs):
        called["n"] += 1
        yield _tool_call_turn(tool_result="Clicked element matching 'Sign In'")
        yield _text_turn("Clicked.\nRESULT: PASS")

    with patch("agent.executor.run_tool_loop", fake_run_tool_loop), patch(
        "agent.executor.verify", new=AsyncMock(return_value=verdict)
    ):
        await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert called["n"] == 1


async def test_run_executor_action_step_fails_when_model_never_calls_a_real_tool():
    """
    Regression test: observed live — the model can narrate an action
    ("I clicked the Login button... RESULT: PASS") in plain text without
    ever issuing a real tool call (e.g. a leaked/malformed tool-call format
    the recovery parser in local_llm.py couldn't salvage). Previously an
    action/navigate step with zero exceptions was unconditionally marked
    OK regardless of whether anything actually happened on the page, which
    let a no-op step cascade into "about:blank" on the next one. A plain
    action step making zero real tool calls must now fail, regardless of
    what the model's text claims.
    """
    steps = [PlannedStep(step_index=1, intent="Click the Sign In button", step_type=StepType.action)]

    with patch(
        "agent.executor.run_tool_loop",
        _fake_run_tool_loop([_text_turn("I clicked the button.\nRESULT: PASS")]),
    ):
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert len(results) == 1
    assert results[0].status == "FAILED"
    assert "never issued a real tool call" in results[0].detail


async def test_run_executor_action_step_fails_when_the_tool_call_itself_reports_failure():
    """
    Regression test: observed live — click()/fill() can return a failure
    STRING (e.g. "Could not find an element matching...") without ever
    raising an exception, and the model can correctly recognize that
    result and end its turn with "...RESULT: FAILED". A real tool call
    DID happen here, so this doesn't hit the "never issued a real tool
    call" branch above — but the step's own final text explicitly says
    FAILED, and the old code ignored that entirely, unconditionally
    marking any action step with a real tool call as OK regardless of
    what the tool actually returned or what the model concluded. That let
    a run whose last step said "the Sign In button could not be found...
    RESULT: FAILED" still roll up to an overall PASSED run.
    """
    steps = [PlannedStep(step_index=1, intent="Click the Sign In button", step_type=StepType.action)]

    with patch(
        "agent.executor.run_tool_loop",
        _fake_run_tool_loop(
            [
                _tool_call_turn(tool_result="Could not find an element matching 'the Sign In button'."),
                _text_turn(
                    'The tool was unable to locate and click the "Sign In" button.\n\nRESULT: FAILED'
                ),
            ]
        ),
    ):
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert len(results) == 1
    assert results[0].status == "FAILED"
    assert "unable to locate" in results[0].detail


async def test_run_executor_retries_first_step_once_when_model_never_calls_a_real_tool():
    """
    Regression test: observed live (AD_LG_03) — same case, same model, same
    prompt, run at a different moment produced a garbled refusal on step 1
    instead of a real navigate() call, while every later step of other
    cases in the same run passed cleanly — genuine per-call model
    non-determinism, not a systemic bug. A bad first roll on step 1
    previously killed the whole case immediately with no recovery attempt,
    unlike mid-run steps which at least inherit prior successful turns as
    context. The executor should give the model one retry on a first-step
    zero-tool-call failure before giving up.
    """
    steps = [PlannedStep(step_index=1, intent="Navigate to the site", step_type=StepType.navigate)]
    call_count = {"n": 0}

    def _fake_run_tool_loop_always_leaked(**kwargs):
        call_count["n"] += 1

        async def fake(**inner_kwargs):
            yield _text_turn("My system functions do not support direct navigation...")

        return fake(**kwargs)

    with patch("agent.executor.run_tool_loop", side_effect=_fake_run_tool_loop_always_leaked):
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert call_count["n"] == 2
    assert len(results) == 1
    assert results[0].status == "FAILED"
    assert "never issued a real tool call" in results[0].detail


async def test_run_executor_retry_succeeds_when_second_attempt_calls_a_real_tool():
    steps = [PlannedStep(step_index=1, intent="Navigate to the site", step_type=StepType.navigate)]

    call_count = {"n": 0}

    def _fake_run_tool_loop_flaky_then_ok(**kwargs):
        call_count["n"] += 1
        this_call = call_count["n"]

        async def fake(**inner_kwargs):
            if this_call == 1:
                yield _text_turn("I navigated there.\nRESULT: PASS")
            else:
                yield _tool_call_turn("navigate", {"url": "https://example.com"}, "Navigated")
                yield _text_turn("Navigated successfully.\nRESULT: PASS")

        return fake(**kwargs)

    with patch("agent.executor.run_tool_loop", side_effect=_fake_run_tool_loop_flaky_then_ok) as mock:
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert mock.call_count == 2
    assert len(results) == 1
    assert results[0].status == "OK"


async def test_run_executor_assertion_step_passes_when_verifier_says_passed():
    """
    The model gives a final answer without ever calling assert_condition —
    the executor should force a real page read (see the "forcing a read
    now" branch in executor.py) and let the Verifier's judgment decide the
    outcome, not the model's own unverified RESULT: PASS/FAILED text.
    """
    steps = [
        PlannedStep(
            step_index=1,
            intent="Assert the dashboard is shown",
            step_type=StepType.assertion,
            expected_outcome="Dashboard is visible",
        )
    ]
    verdict = VerificationResult(status="passed", confidence=0.9, explanation="Dashboard heading is present.")

    with patch(
        "agent.executor.run_tool_loop",
        _fake_run_tool_loop([_text_turn("The dashboard is visible.\nRESULT: PASS")]),
    ), patch("agent.executor.verify", new=AsyncMock(return_value=verdict)) as mock_verify:
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert results[0].status == "OK"
    mock_verify.assert_awaited_once()


async def test_run_executor_assertion_step_fails_and_skips_remaining_steps():
    steps = [
        PlannedStep(
            step_index=1, intent="Assert something", step_type=StepType.assertion,
            expected_outcome="Something is true",
        ),
        PlannedStep(step_index=2, intent="A later step that should be skipped", step_type=StepType.action),
    ]
    verdict = VerificationResult(status="failed", confidence=0.85, explanation="Not visible.")

    with patch(
        "agent.executor.run_tool_loop", _fake_run_tool_loop([_text_turn("Not visible.\nRESULT: FAILED")])
    ), patch("agent.executor.verify", new=AsyncMock(return_value=verdict)):
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert len(results) == 2
    assert results[0].status == "FAILED"
    assert results[1].status == "FAILED"
    assert "Skipped" in results[1].detail


async def test_run_executor_forces_a_page_read_when_model_never_asserts():
    """
    Regression test: previously, an assertion step whose model never called
    assert_condition would fall straight to text-marker parsing and could
    end up FAILED with an empty, unhelpful detail. The executor now forces
    one real read so the Verifier always has actual page state to judge.
    """
    steps = [
        PlannedStep(
            step_index=1, intent="Assert something", step_type=StepType.assertion,
            expected_outcome="Something is true",
        )
    ]
    verdict = VerificationResult(status="failed", confidence=0.6, explanation="Could not confirm.")

    with patch(
        "agent.executor.run_tool_loop",
        _fake_run_tool_loop([_text_turn("I looked at the page but forgot the format.")]),
    ), patch("agent.executor.verify", new=AsyncMock(return_value=verdict)) as mock_verify:
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert results[0].status == "FAILED"
    assert results[0].detail == "Could not confirm."
    mock_verify.assert_awaited_once()


async def test_run_executor_carries_post_action_snapshot_into_next_step_verification():
    """
    Regression test for the transient-toast bug: a click's result can
    render and clear itself before the model gets around to calling
    assert_condition on the next step. The executor should carry whatever
    ctx.post_action_snapshot was set to by the action step into the very
    next step's verify() call, then clear it so it doesn't leak further.
    """
    steps = [
        PlannedStep(step_index=1, intent="Click Sign In", step_type=StepType.action),
        PlannedStep(
            step_index=2, intent="Assert an error is shown", step_type=StepType.assertion,
            expected_outcome="An error message is shown",
        ),
    ]
    verdict = VerificationResult(status="passed", confidence=1.0, explanation="Toast was shown.")

    call_count = {"n": 0}

    def _fake_run_tool_loop_with_side_effects(**kwargs):
        call_count["n"] += 1
        this_call = call_count["n"]

        async def fake(**inner_kwargs):
            tools = inner_kwargs["tools"]
            if this_call == 1:
                # Step 1 (action): simulate the click tool populating
                # post_action_snapshot, same as the real click() handler.
                click_tool = next(t for t in tools if t.name == "click")
                result = await click_tool(selector_hint="Sign In")
                yield _tool_call_turn("click", {"selector_hint": "Sign In"}, result)
                yield _text_turn("Clicked the button.\nRESULT: PASS")
            else:
                # Step 2 (assertion): simulate the model calling
                # assert_condition itself, which sets last_snapshot to a
                # LATER, staler read — this is the exact overwrite that
                # broke the original fix.
                assert_tool = next(t for t in tools if t.name == "assert_condition")
                result = await assert_tool(description="An error message is shown")
                yield _tool_call_turn("assert_condition", {"description": "An error message is shown"}, result)
                yield _text_turn("Checked the page.\nRESULT: FAILED")

        return fake(**kwargs)

    def _fake_build_tools(ctx):
        async def fake_click(selector_hint: str = "") -> str:
            ctx.post_action_snapshot = "Invalid credentials toast visible"
            return "Clicked"
        fake_click.name = "click"

        async def fake_assert_condition(description: str = "") -> str:
            ctx.last_snapshot = "dashboard, no toast (arrived late)"
            return ctx.last_snapshot
        fake_assert_condition.name = "assert_condition"

        return [fake_click, fake_assert_condition]

    with patch(
        "agent.executor.run_tool_loop", side_effect=_fake_run_tool_loop_with_side_effects
    ), patch("agent.executor.build_tools", side_effect=_fake_build_tools), patch(
        "agent.executor.verify", new=AsyncMock(return_value=verdict)
    ) as mock_verify:
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    assert results[1].status == "OK"
    mock_verify.assert_awaited_once()
    call_kwargs = mock_verify.call_args.kwargs
    assert call_kwargs["post_action_dom_state"] == "Invalid credentials toast visible"
    assert mock_verify.call_args.args[1] == "dashboard, no toast (arrived late)"


async def test_run_executor_stops_and_skips_remaining_on_exception():
    from agent.local_llm import LocalLLMError

    steps = [
        PlannedStep(step_index=1, intent="Step that will error", step_type=StepType.action),
        PlannedStep(step_index=2, intent="Never reached", step_type=StepType.action),
    ]

    def factory(**kwargs):
        raise LocalLLMError("simulated local model connection failure")

    with patch("agent.executor.run_tool_loop", factory), patch(
        "agent.executor.asyncio.sleep", new=AsyncMock()
    ):
        results = await run_executor(
            MagicMock(), InMemoryStore(), EventBus(), "run1", "TC-TEST", steps, log=lambda msg: None
        )

    # A tool-loop exception is retried (with backoff) before being treated as
    # a genuine infra error — see docs/phase-04-verifier-and-reliability.md.
    assert len(results) == 2
    assert results[0].status == "ERROR"
    assert "Executor error after 3 attempt(s)" in results[0].detail
    assert "Skipped" in results[1].detail


async def test_run_executor_records_agent_traces():
    store = InMemoryStore()
    steps = [PlannedStep(step_index=1, intent="Click something", step_type=StepType.action)]

    with patch("agent.executor.run_tool_loop", _fake_run_tool_loop([_text_turn("Done.")])):
        await run_executor(MagicMock(), store, EventBus(), "run42", "TC-TEST", steps, log=lambda msg: None)

    traces = store.get_agent_traces("run42")
    roles = [t.role for t in traces]
    message_types = [t.message_type for t in traces]
    assert "planner" in roles
    assert "executor" in roles
    assert "final_answer" in message_types
