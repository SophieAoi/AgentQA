"""
Regression coverage for agent/runner.py::_fix_hallucinated_urls() — observed
live: the Planner can paraphrase a real URL it was given verbatim
(influence-stg.movingwalls.com) into a plausible-looking but wrong one
(e.g. a *.example.com placeholder), which then fails DNS resolution and
takes the rest of the test case down with it.
"""

from unittest.mock import patch

from agent.runner import _fix_hallucinated_urls
from app.models.schemas import PlannedStep, StepType


def test_corrects_a_navigate_step_pointing_at_the_wrong_host():
    steps = [
        PlannedStep(
            step_index=0,
            intent="Navigate to https://influence-staging.example.com",
            step_type=StepType.navigate,
        )
    ]
    logs = []

    with patch("agent.runner.INFLUENCE_BASE_URL", "https://influence-stg.movingwalls.com"), patch(
        "agent.runner._INFLUENCE_HOST", "influence-stg.movingwalls.com"
    ):
        _fix_hallucinated_urls(steps, logs.append)

    assert steps[0].intent == "Navigate to https://influence-stg.movingwalls.com"
    assert len(logs) == 1
    assert "influence-staging.example.com" in logs[0]


def test_leaves_a_navigate_step_pointing_at_the_right_host_untouched():
    steps = [
        PlannedStep(
            step_index=0,
            intent="Navigate to https://influence-stg.movingwalls.com",
            step_type=StepType.navigate,
        )
    ]
    logs = []

    with patch("agent.runner.INFLUENCE_BASE_URL", "https://influence-stg.movingwalls.com"), patch(
        "agent.runner._INFLUENCE_HOST", "influence-stg.movingwalls.com"
    ):
        _fix_hallucinated_urls(steps, logs.append)

    assert steps[0].intent == "Navigate to https://influence-stg.movingwalls.com"
    assert logs == []


def test_leaves_non_navigate_steps_untouched_even_with_a_wrong_looking_url():
    steps = [
        PlannedStep(
            step_index=0,
            intent="Assert the page mentions https://influence-staging.example.com",
            step_type=StepType.assertion,
            expected_outcome="the URL is mentioned",
        )
    ]
    logs = []

    with patch("agent.runner.INFLUENCE_BASE_URL", "https://influence-stg.movingwalls.com"), patch(
        "agent.runner._INFLUENCE_HOST", "influence-stg.movingwalls.com"
    ):
        _fix_hallucinated_urls(steps, logs.append)

    assert "example.com" in steps[0].intent
    assert logs == []


def test_leaves_a_navigate_step_with_no_url_untouched():
    steps = [
        PlannedStep(step_index=0, intent="Navigate to the login page", step_type=StepType.navigate)
    ]
    logs = []

    with patch("agent.runner.INFLUENCE_BASE_URL", "https://influence-stg.movingwalls.com"), patch(
        "agent.runner._INFLUENCE_HOST", "influence-stg.movingwalls.com"
    ):
        _fix_hallucinated_urls(steps, logs.append)

    assert steps[0].intent == "Navigate to the login page"
    assert logs == []
