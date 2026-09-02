from unittest.mock import AsyncMock, patch

import pytest

import agent.planner as planner_module
from agent.local_llm import LocalLLMError
from agent.planner import PlannedStepList, PlannerError, plan_test_case
from app.models.schemas import PlannedStep, StepType


@pytest.fixture(autouse=True)
def _clear_plan_cache():
    """
    plan_test_case()'s cache is process-wide by design (see its docstring —
    the whole point is surviving across separate calls, unlike
    SelectorCache which is deliberately per-run). Several tests below reuse
    the same literal description string (e.g. "Do something"); without
    clearing between tests, a later test could silently hit an earlier
    test's cached result and skip its own mock entirely instead of
    exercising the code path it's actually meant to test.
    """
    planner_module._plan_cache.clear()
    yield
    planner_module._plan_cache.clear()


async def test_plan_test_case_returns_parsed_steps():
    expected_steps = [
        PlannedStep(step_index=1, intent="Click the Sign In button", step_type=StepType.action),
        PlannedStep(
            step_index=2,
            intent="Assert the dashboard is shown",
            step_type=StepType.assertion,
            expected_outcome="The dashboard page is visible",
        ),
    ]

    with patch(
        "agent.planner.structured_chat",
        new=AsyncMock(return_value=PlannedStepList(steps=expected_steps)),
    ) as mock_structured_chat:
        steps = await plan_test_case("Log in and check the dashboard")

    assert steps == expected_steps
    call_kwargs = mock_structured_chat.call_args.kwargs
    assert call_kwargs["output_model"] is PlannedStepList
    assert "Log in and check the dashboard" in call_kwargs["user_message"]


async def test_plan_test_case_includes_page_context_when_given():
    with patch(
        "agent.planner.structured_chat",
        new=AsyncMock(
            return_value=PlannedStepList(
                steps=[PlannedStep(step_index=1, intent="Click something", step_type=StepType.action)]
            )
        ),
    ) as mock_structured_chat:
        await plan_test_case("Do a thing", page_context="Currently on the Deal Desk page")

    sent_content = mock_structured_chat.call_args.kwargs["user_message"]
    assert "Currently on the Deal Desk page" in sent_content


async def test_plan_test_case_raises_on_empty_plan():
    with patch(
        "agent.planner.structured_chat", new=AsyncMock(return_value=PlannedStepList(steps=[]))
    ):
        with pytest.raises(PlannerError, match="empty plan"):
            await plan_test_case("Do something")


async def test_plan_test_case_wraps_local_llm_error():
    with patch(
        "agent.planner.structured_chat",
        new=AsyncMock(side_effect=LocalLLMError("could not reach local model")),
    ):
        with pytest.raises(PlannerError, match="Planner call failed"):
            await plan_test_case("Do something")


async def test_plan_test_case_translates_a_leaked_intent_and_expected_outcome():
    """
    The structured-output JSON schema constrains PlannedStep's shape, not
    its language — intent (shown as the step description in the UI) and
    expected_outcome (fed into the Verifier's prompt) are free-text fields
    that can still leak non-English text despite the system prompt's
    explicit instruction.
    """
    leaked_steps = [
        PlannedStep(step_index=1, intent="点击登录按钮", step_type=StepType.action),
        PlannedStep(
            step_index=2,
            intent="Verify login failed",
            step_type=StepType.assertion,
            expected_outcome="显示了错误消息",
        ),
    ]

    async def fake_translate(text, model=None):
        return {"点击登录按钮": "Click the Login button", "显示了错误消息": "An error message is shown"}[text]

    with patch(
        "agent.planner.structured_chat", new=AsyncMock(return_value=PlannedStepList(steps=leaked_steps))
    ), patch("agent.planner._translate_to_english", new=AsyncMock(side_effect=fake_translate)):
        steps = await plan_test_case("Log in with invalid credentials")

    assert steps[0].intent == "Click the Login button"
    assert steps[1].intent == "Verify login failed"  # already English, untouched
    assert steps[1].expected_outcome == "An error message is shown"


async def test_plan_test_case_does_not_translate_english_steps():
    steps_in = [PlannedStep(step_index=1, intent="Click the Login button", step_type=StepType.action)]

    with patch(
        "agent.planner.structured_chat", new=AsyncMock(return_value=PlannedStepList(steps=steps_in))
    ), patch("agent.planner._translate_to_english", new=AsyncMock()) as mock_translate:
        steps = await plan_test_case("Log in")

    assert steps[0].intent == "Click the Login button"
    mock_translate.assert_not_awaited()


async def test_plan_test_case_skips_the_llm_call_on_a_repeat_of_the_same_description():
    """
    A test case's YAML description never changes between separate runs of
    the same case, so re-paying a full Planner LLM call to regenerate an
    identical plan on every run is pure waste (5-10+s locally). The second
    call with the same description must return the cached plan without
    ever invoking structured_chat again.
    """
    steps = [PlannedStep(step_index=1, intent="Click Sign In", step_type=StepType.action)]

    with patch(
        "agent.planner.structured_chat", new=AsyncMock(return_value=PlannedStepList(steps=steps))
    ) as mock_structured_chat:
        first = await plan_test_case("Log in with valid credentials")
        second = await plan_test_case("Log in with valid credentials")

    assert first == second
    mock_structured_chat.assert_awaited_once()


async def test_plan_test_case_cache_does_not_leak_mutations_between_runs():
    """
    executor.py/runner.py mutate a PlannedStep's fields in place after
    getting them back (translation, _fix_hallucinated_urls) — the cache
    must return a fresh copy each time, not the same object reused across
    calls, or one run's in-place edit would corrupt what every future run
    of that case sees.
    """
    steps = [PlannedStep(step_index=1, intent="Click Sign In", step_type=StepType.action)]

    with patch("agent.planner.structured_chat", new=AsyncMock(return_value=PlannedStepList(steps=steps))):
        first = await plan_test_case("Log in with valid credentials")
        first[0].intent = "mutated by caller"
        second = await plan_test_case("Log in with valid credentials")

    assert second[0].intent == "Click Sign In"


async def test_plan_test_case_cache_key_includes_page_context():
    """Different page_context legitimately warrants a different plan — a
    cache hit purely on description alone would silently return a plan
    generated for the wrong page context."""
    steps_a = [PlannedStep(step_index=1, intent="Plan A", step_type=StepType.action)]
    steps_b = [PlannedStep(step_index=1, intent="Plan B", step_type=StepType.action)]

    with patch(
        "agent.planner.structured_chat",
        new=AsyncMock(side_effect=[PlannedStepList(steps=steps_a), PlannedStepList(steps=steps_b)]),
    ) as mock_structured_chat:
        first = await plan_test_case("Do a thing", page_context="On page A")
        second = await plan_test_case("Do a thing", page_context="On page B")

    assert first[0].intent == "Plan A"
    assert second[0].intent == "Plan B"
    assert mock_structured_chat.await_count == 2
