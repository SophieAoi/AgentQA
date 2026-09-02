"""
Verifier — formalizes the assertion-judgment step that was implicit in the
Executor's RESULT: PASS/FAILED marker convention since phase 3 (see
docs/BUILD-PLAN.md § "Why two agent roles"). A single structured-output
call, not a tool-use loop: verification is a narrower, classification-like
task compared to the Executor's open-ended execution, so it's a natural
place to allow a distinct (possibly smaller/faster) local model —
VERIFIER_MODEL, defaulting to the same model the Executor uses.

Majority-vote judging: confirmed live, repeatedly — the same real DOM
state and expected outcome, judged by the same model on separate calls,
occasionally gets one call wrong even though a 5x retest comes back 5/5
correct every other time (e.g. AD_LG_01 flip-flopping pass/fail across
runs with zero code changes in between; the model's own quoted evidence
sometimes contradicts its own verdict). This is real per-call
non-determinism in the model's reasoning, not a prompt-wording gap (the
prompt has already been tightened for the specific patterns found), so no
amount of further test-case or prompt editing removes it. Voting is the
direct mitigation: call _evaluate_single more than once on the SAME DOM
state and require agreement, so a single bad roll doesn't decide the
whole step's outcome by itself.
"""

from agent.flakiness_tracker import is_known_flaky, record_tie_break, should_sample_vote
from agent.local_llm import LocalLLMError, _looks_leaked, _translate_to_english, structured_chat
from app.config import VERIFIER_MODEL
from app.models.schemas import VerificationResult

VERIFIER_SYSTEM_PROMPT = """You are the verification stage of an automated QA agent. You are \
given an expected outcome — what a test step is supposed to prove — and the actual state of a \
web page after the step ran. Judge whether the expected outcome holds.

Judge ONLY the specific expected outcome you were given — nothing else about the page matters. \
Real pages contain plenty of content that has nothing to do with what you're checking (marketing \
copy, unrelated headings, sidebars, stats, other UI chrome) — this is normal and does not make the \
page state ambiguous or contradictory. Do not flag unrelated content as a "mismatch" or an \
"inconsistency," do not treat it as a reason to lower your confidence, and do not describe the \
page as looking like "mixed content" or "not what you'd expect for X" because of it. Your only job \
is: does the page state contain evidence of the specific expected outcome, yes or no.

Be precise about that one question: only mark status "passed" if the page state actually \
demonstrates the expected outcome — do not assume, infer beyond what's shown, or give the benefit \
of the doubt. If the page state genuinely doesn't contain enough information to judge THAT \
specific outcome (not "does something else look unusual"), mark status "failed" and use a lower \
confidence, explaining what was missing.

When the expected outcome is about "an error message" (or similarly generic phrasing — "a \
validation error," "an alert," "feedback is shown"), do not require the exact word "error" or a \
specific visual style to be present. ANY short, on-screen text stating the action didn't succeed — \
"Invalid credentials," "This field is required," "Something went wrong," a red/highlighted message \
near a form field, etc. — counts as the error message being shown. If you find yourself concluding \
the page state contains text like this but reasoning that it's "not really an error message" or \
"a different kind of feedback" rather than the one the outcome describes, that reasoning is wrong — \
mark it passed. The specific wording of the message is never itself a reason to fail this kind of \
check; only its complete absence is.

confidence reflects how certain you are in your status judgment (1.0 = certain, 0.5 = genuinely \
ambiguous), not how good the outcome was.

The page state is an accessibility-tree snapshot, not a visual layout — confirmed live, adjacent \
unlabeled text nodes routinely get concatenated onto a single "text:" line with just a space \
between them and no punctuation, even when they come from completely different, unrelated parts of \
the real page (e.g. a validation message under one field and the NEXT field's label can appear \
merged as one line, like "X is required Y *"). This is a normal, expected artifact of how the \
snapshot is generated — it is NEVER evidence that the merged text doesn't really exist, isn't in a \
"separate line/element," or is somehow suspect. If the exact phrase you're checking for appears \
anywhere in the page state as a literal substring — merged with other text or not — that fully \
counts as it being present; do not fail a check or lower confidence because the surrounding text \
looks "merged," lacks its own line, or lacks a clear visual boundary in the snapshot. Judge \
presence purely by whether the phrase's exact wording appears in the text; never by its formatting, \
its neighbors, or how it's laid out in the snapshot.

Always respond in English, regardless of the language of the page state you are judging."""


class VerifierError(RuntimeError):
    """Raised when the verifier can't run at all (e.g. local model unreachable)."""


async def _evaluate_single_call(expected_outcome: str, dom_state: str) -> VerificationResult:
    """Exactly one real model call — no voting. Used by _evaluate_single
    (the voting wrapper below) as its building block."""
    try:
        result = await structured_chat(
            system=VERIFIER_SYSTEM_PROMPT,
            user_message=f"Expected outcome: {expected_outcome}\n\nActual page state:\n{dom_state}",
            output_model=VerificationResult,
            model=VERIFIER_MODEL,
        )
    except LocalLLMError as exc:
        raise VerifierError(f"Verifier call failed: {exc}") from exc

    # The JSON schema constrains the response's shape, not its language —
    # explanation is a free-text field, so it can still leak non-English
    # text despite the prompt's explicit instruction. Same cosmetic-only
    # fix as the Executor's tool loop: status/confidence (the fields that
    # actually drive pass/fail logic) are untouched either way.
    if _looks_leaked(result.explanation):
        result.explanation = await _translate_to_english(result.explanation, VERIFIER_MODEL)
    return result


async def _evaluate_single(
    expected_outcome: str, dom_state: str, test_case_id: str | None = None
) -> VerificationResult:
    """
    Majority-vote wrapper around _evaluate_single_call. Votes when EITHER
    the case is already known-flaky (flakiness_tracker.is_known_flaky —
    repeated observed tie-breaks) OR a random sample check fires for a
    not-yet-flagged case (flakiness_tracker.should_sample_vote — without
    this, a case could never accumulate its first tie-break, since
    nothing would ever call it more than once to find out). Every other
    run takes the original single-call fast path. Voting on every case
    unconditionally doubled the cost of every assertion step regardless
    of whether that specific case ever showed inconsistency; gating it
    keeps the speed cost paid mostly where it's earning its keep, with
    occasional sampling elsewhere to discover new flakiness over time.
    test_case_id=None (e.g. a call site that hasn't been updated to pass
    it) degrades to "never known-flaky, still sampled occasionally" — the
    safe default, not a crash.

    When it does vote: calls _evaluate_single_call twice on the SAME
    dom_state/expected_outcome; if both agree on status, that's the
    answer (2 calls). If they disagree, a third call breaks the tie (3
    calls total) AND records the disagreement via record_tie_break() —
    the real signal that grows a case's flakiness count in the first
    place, so this only self-reinforces on genuine, repeated inconsistency,
    not on every vote regardless of outcome.
    """
    if not is_known_flaky(test_case_id) and not should_sample_vote():
        return await _evaluate_single_call(expected_outcome, dom_state)

    first = await _evaluate_single_call(expected_outcome, dom_state)
    second = await _evaluate_single_call(expected_outcome, dom_state)
    if first.status == second.status:
        # Agreement — return whichever call carries the higher confidence,
        # since both reached the same verdict.
        return first if first.confidence >= second.confidence else second

    record_tie_break(test_case_id)
    tiebreaker = await _evaluate_single_call(expected_outcome, dom_state)
    votes = [first, second, tiebreaker]
    passed_votes = [v for v in votes if v.status == "passed"]
    failed_votes = [v for v in votes if v.status == "failed"]
    winning_votes = passed_votes if len(passed_votes) >= len(failed_votes) else failed_votes
    # Among the votes on the winning side, prefer the most confident one
    # as the representative explanation shown to the user.
    return max(winning_votes, key=lambda v: v.confidence)


async def evaluate(
    expected_outcome: str,
    actual_dom_state: str,
    post_action_dom_state: str | None = None,
    test_case_id: str | None = None,
) -> VerificationResult:
    """
    When a post_action_dom_state is available (captured right after the
    triggering action, e.g. a click — see ToolContext.post_action_snapshot
    in agent/tools/playwright_tools.py) and differs from the later
    actual_dom_state, each snapshot is judged with its OWN, independent
    verifier call rather than one call given both. This was tried first as
    a single call shown both snapshots with instructions to pass if
    EITHER demonstrates the outcome — but the model kept overriding that
    instruction with its own "the snapshots should be consistent" logic,
    marking status "failed" even after its own explanation confirmed the
    earlier snapshot showed the expected outcome (observed live: a
    transient "Invalid credentials" toast visible in the first snapshot,
    gone by the second — correctly judged as passed by an isolated call
    against the first snapshot alone, then wrongly failed once the second,
    contradicting snapshot was shown alongside it in the same call).
    Splitting into two single-snapshot calls removes that failure mode
    entirely: each call has no idea a second snapshot even exists, so
    there's nothing for it to weigh against. The two results are then
    OR'd in code — deterministic, not a second model judgment call.

    Note: "single call" here means "one independent verdict for one DOM
    state" from this function's perspective — _evaluate_single() itself
    conditionally makes 2-3 real model calls (majority vote, gated on
    flakiness_tracker's known-flaky/sample-check triggers — see its own
    docstring) to guard against a single bad roll deciding that verdict.
    """
    if not post_action_dom_state or post_action_dom_state == actual_dom_state:
        return await _evaluate_single(expected_outcome, actual_dom_state, test_case_id)

    early = await _evaluate_single(expected_outcome, post_action_dom_state, test_case_id)
    if early.status == "passed":
        return early

    later = await _evaluate_single(expected_outcome, actual_dom_state, test_case_id)
    if later.status == "passed":
        return later

    # Neither snapshot demonstrated it — prefer whichever explanation
    # carries more certainty rather than always defaulting to one side.
    return later if later.confidence >= early.confidence else early
