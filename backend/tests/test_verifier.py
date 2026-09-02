from unittest.mock import AsyncMock, patch

import pytest

from agent.local_llm import LocalLLMError
from agent.verifier import VerifierError, evaluate
from app.models.schemas import VerificationResult

# Every test below patches agent.verifier.is_known_flaky and/or
# should_sample_vote explicitly, forcing the vote/no-vote decision
# deterministically — flakiness_tracker.py's real random sampling
# (should_sample_vote) would otherwise make these tests themselves flaky.


async def test_evaluate_returns_passed_with_confidence():
    result = VerificationResult(status="passed", confidence=0.95, explanation="Dashboard heading is present.")

    with patch(
        "agent.verifier.structured_chat", new=AsyncMock(return_value=result)
    ) as mock_structured_chat, patch("agent.verifier.should_sample_vote", return_value=False):
        verdict = await evaluate("Dashboard is visible", "page shows Dashboard heading")

    assert verdict.status == "passed"
    assert verdict.confidence == 0.95
    call_kwargs = mock_structured_chat.call_args.kwargs
    assert call_kwargs["output_model"] is VerificationResult
    assert "Dashboard is visible" in call_kwargs["user_message"]
    assert "page shows Dashboard heading" in call_kwargs["user_message"]


async def test_evaluate_returns_failed_with_low_confidence_when_ambiguous():
    result = VerificationResult(
        status="failed", confidence=0.4, explanation="Page state doesn't clearly show the expected element."
    )

    with patch("agent.verifier.structured_chat", new=AsyncMock(return_value=result)), patch(
        "agent.verifier.should_sample_vote", return_value=False
    ):
        verdict = await evaluate("Something is true", "unrelated page content")

    assert verdict.status == "failed"
    assert verdict.confidence == 0.4


async def test_evaluate_uses_configured_verifier_model():
    result = VerificationResult(status="passed", confidence=1.0, explanation="Matches.")

    with patch(
        "agent.verifier.structured_chat", new=AsyncMock(return_value=result)
    ) as mock_structured_chat, patch("agent.verifier.VERIFIER_MODEL", "qwen2.5:14b-instruct"), patch(
        "agent.verifier.should_sample_vote", return_value=False
    ):
        await evaluate("expected", "actual")

    assert mock_structured_chat.call_args.kwargs["model"] == "qwen2.5:14b-instruct"


async def test_evaluate_wraps_connection_failure():
    with patch(
        "agent.verifier.structured_chat",
        new=AsyncMock(side_effect=LocalLLMError("could not reach the local model")),
    ), patch("agent.verifier.should_sample_vote", return_value=False):
        with pytest.raises(VerifierError, match="Verifier call failed"):
            await evaluate("expected", "actual")


async def test_evaluate_wraps_schema_validation_failure():
    with patch(
        "agent.verifier.structured_chat",
        new=AsyncMock(side_effect=LocalLLMError("response did not match the expected schema")),
    ), patch("agent.verifier.should_sample_vote", return_value=False):
        with pytest.raises(VerifierError, match="Verifier call failed"):
            await evaluate("expected", "actual")


async def test_evaluate_omits_a_second_snapshot_call_when_post_action_snapshot_absent_or_same():
    """
    Two DIFFERENT things both called "calls" here — disambiguated by
    counting structured_chat's real await_count (majority-vote calls
    included) vs. asserting there's only ONE snapshot being judged at all
    (no post-action/actual split). A single DOM state costs 2 real calls
    now that _evaluate_single votes — see the dedicated voting tests below
    for that mechanism in isolation.
    """
    result = VerificationResult(status="passed", confidence=1.0, explanation="Matches.")

    with patch(
        "agent.verifier.structured_chat", new=AsyncMock(return_value=result)
    ) as mock_structured_chat, patch("agent.verifier.should_sample_vote", return_value=True):
        await evaluate("expected", "actual", post_action_dom_state=None)

    assert mock_structured_chat.await_count == 2  # one DOM state, voted twice (agreement)

    with patch(
        "agent.verifier.structured_chat", new=AsyncMock(return_value=result)
    ) as mock_structured_chat, patch("agent.verifier.should_sample_vote", return_value=True):
        await evaluate("expected", "actual", post_action_dom_state="actual")

    assert mock_structured_chat.await_count == 2


async def test_evaluate_passes_when_the_earlier_snapshot_alone_shows_the_outcome():
    """
    Regression test: previously a single verifier call was shown BOTH
    snapshots at once with instructions to pass if either demonstrated the
    outcome — but the model kept overriding that with its own "the two
    should be consistent" reasoning, marking status FAILED even after its
    own explanation confirmed the earlier snapshot showed a transient
    'Invalid credentials' message that had cleared by the later snapshot.
    Splitting into two independent single-snapshot calls (each with no
    idea a second snapshot exists) and OR-ing the results in code removes
    that failure mode: each call can only judge the one snapshot it sees.
    Both votes on the early snapshot agree here, so the early group short-
    circuits evaluate() without ever spending a call on the later snapshot.
    """
    early_result = VerificationResult(status="passed", confidence=1.0, explanation="Toast visible here.")
    later_result = VerificationResult(status="failed", confidence=0.9, explanation="No toast here.")

    with patch(
        "agent.verifier.structured_chat",
        new=AsyncMock(side_effect=[early_result, early_result, later_result, later_result]),
    ) as mock_structured_chat, patch("agent.verifier.should_sample_vote", return_value=True):
        verdict = await evaluate(
            "An error message is shown",
            "page shows the dashboard, no error",
            post_action_dom_state="page shows 'Invalid credentials' toast",
        )

    assert verdict.status == "passed"
    # Short-circuits once the early snapshot's vote passes — no need to
    # also spend calls checking the later one.
    assert mock_structured_chat.await_count == 2
    call_kwargs = mock_structured_chat.call_args.kwargs
    assert "Invalid credentials" in call_kwargs["user_message"]


async def test_evaluate_falls_back_to_the_later_snapshot_when_earlier_one_fails():
    early_result = VerificationResult(status="failed", confidence=0.8, explanation="Nothing here yet.")
    later_result = VerificationResult(status="passed", confidence=1.0, explanation="Now it's shown.")

    with patch(
        "agent.verifier.structured_chat",
        new=AsyncMock(side_effect=[early_result, early_result, later_result, later_result]),
    ) as mock_structured_chat, patch("agent.verifier.should_sample_vote", return_value=True):
        verdict = await evaluate(
            "An error message is shown",
            "page shows the error now",
            post_action_dom_state="page shows nothing yet",
        )

    assert verdict.status == "passed"
    assert mock_structured_chat.await_count == 4  # 2 votes on early (fails) + 2 votes on later (passes)


async def test_evaluate_fails_when_neither_snapshot_shows_the_outcome():
    early_result = VerificationResult(status="failed", confidence=0.6, explanation="No error here.")
    later_result = VerificationResult(status="failed", confidence=0.9, explanation="Still no error.")

    with patch(
        "agent.verifier.structured_chat",
        new=AsyncMock(side_effect=[early_result, early_result, later_result, later_result]),
    ), patch("agent.verifier.should_sample_vote", return_value=True):
        verdict = await evaluate(
            "An error message is shown",
            "page shows the dashboard",
            post_action_dom_state="page shows the login form, no error",
        )

    assert verdict.status == "failed"
    # Prefers whichever group's verdict carries more confidence when both fail.
    assert verdict.explanation == "Still no error."


async def test_evaluate_single_returns_the_immediate_verdict_when_both_votes_agree():
    passed_result = VerificationResult(status="passed", confidence=0.8, explanation="First vote.")
    passed_result_2 = VerificationResult(status="passed", confidence=0.95, explanation="Second vote.")

    with patch(
        "agent.verifier.structured_chat", new=AsyncMock(side_effect=[passed_result, passed_result_2])
    ) as mock_structured_chat, patch("agent.verifier.should_sample_vote", return_value=True):
        verdict = await evaluate("expected", "actual")

    assert verdict.status == "passed"
    assert mock_structured_chat.await_count == 2
    # Prefers the higher-confidence vote among the two that agreed.
    assert verdict.explanation == "Second vote."


async def test_evaluate_single_breaks_a_tie_with_a_third_vote():
    """
    Direct regression coverage for the actual bug this feature targets:
    observed live, repeatedly — the same real DOM state and expected
    outcome, judged by the same model on separate calls, occasionally
    disagrees with itself even though a same-input retest comes back
    correct almost every time (AD_LG_01 flip-flopping pass/fail with zero
    code changes in between). A single bad roll must not decide the whole
    step by itself — when the first two votes disagree, a third vote
    breaks the tie by majority.
    """
    wrong_fail = VerificationResult(status="failed", confidence=0.7, explanation="The one bad roll.")
    correct_pass_1 = VerificationResult(status="passed", confidence=1.0, explanation="Correct read #1.")
    correct_pass_2 = VerificationResult(status="passed", confidence=1.0, explanation="Correct read #2.")

    with patch(
        "agent.verifier.structured_chat",
        new=AsyncMock(side_effect=[wrong_fail, correct_pass_1, correct_pass_2]),
    ) as mock_structured_chat, patch("agent.verifier.should_sample_vote", return_value=True):
        verdict = await evaluate("expected", "actual")

    assert verdict.status == "passed"  # 2 of 3 votes correctly say passed
    assert mock_structured_chat.await_count == 3


async def test_evaluate_votes_for_a_known_flaky_case_even_without_sampling():
    """
    The gate is an OR of two independent triggers — is_known_flaky() and
    should_sample_vote() — and only the sampling branch had direct coverage
    so far. This proves the other branch works standalone: a case already
    flagged flaky must still vote even when the random sample check would
    have skipped it.
    """
    passed_result = VerificationResult(status="passed", confidence=0.8, explanation="First vote.")
    passed_result_2 = VerificationResult(status="passed", confidence=0.95, explanation="Second vote.")

    with patch(
        "agent.verifier.structured_chat", new=AsyncMock(side_effect=[passed_result, passed_result_2])
    ) as mock_structured_chat, patch("agent.verifier.should_sample_vote", return_value=False), patch(
        "agent.verifier.is_known_flaky", return_value=True
    ):
        verdict = await evaluate("expected", "actual", test_case_id="TC-KNOWN-FLAKY")

    assert verdict.status == "passed"
    assert mock_structured_chat.await_count == 2


async def test_evaluate_single_tiebreaker_can_also_confirm_a_failure():
    correct_fail_1 = VerificationResult(status="failed", confidence=0.9, explanation="Genuinely missing.")
    wrong_pass = VerificationResult(status="passed", confidence=0.6, explanation="The one bad roll.")
    correct_fail_2 = VerificationResult(status="failed", confidence=0.85, explanation="Still genuinely missing.")

    with patch(
        "agent.verifier.structured_chat",
        new=AsyncMock(side_effect=[correct_fail_1, wrong_pass, correct_fail_2]),
    ), patch("agent.verifier.should_sample_vote", return_value=True):
        verdict = await evaluate("expected", "actual")

    assert verdict.status == "failed"  # 2 of 3 votes correctly say failed


async def test_evaluate_translates_a_leaked_explanation():
    """
    The verifier's structured-output JSON schema constrains the response's
    shape, not its language — explanation is a free-text field and can
    still leak non-English text (observed live) despite the system
    prompt's explicit "always respond in English" instruction.
    """
    leaked_result = VerificationResult(
        status="passed", confidence=1.0, explanation="页面显示了错误消息'Invalid credentials'。"
    )

    with patch(
        "agent.verifier.structured_chat", new=AsyncMock(return_value=leaked_result)
    ), patch(
        "agent.verifier._translate_to_english",
        new=AsyncMock(return_value="The page shows the error message 'Invalid credentials'."),
    ) as mock_translate, patch("agent.verifier.should_sample_vote", return_value=False):
        verdict = await evaluate("An error message is shown", "page shows the error")

    assert verdict.explanation == "The page shows the error message 'Invalid credentials'."
    mock_translate.assert_awaited_once()


async def test_evaluate_does_not_translate_an_english_explanation():
    result = VerificationResult(status="passed", confidence=1.0, explanation="Error message is present.")

    with patch(
        "agent.verifier.structured_chat", new=AsyncMock(return_value=result)
    ), patch("agent.verifier._translate_to_english", new=AsyncMock()) as mock_translate, patch(
        "agent.verifier.should_sample_vote", return_value=False
    ):
        verdict = await evaluate("An error message is shown", "page shows the error")

    assert verdict.explanation == "Error message is present."
    mock_translate.assert_not_awaited()
