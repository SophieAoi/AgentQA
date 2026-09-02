# Phase 4 — Verifier hardening + reliability pass

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context and design rationale.

## Goal

Formalize the verification responsibility that's been implicit in `assert_condition` since phase 3, and close reliability gaps (F-005-style stuck-running states, ambiguous pass/fail, no confidence signal) before reporting (phase 6) is built on top of possibly-flaky data.

## Scope — build

- Promote assertion evaluation into an explicit `agent/verifier.py::evaluate(expected_outcome, actual_dom_state) -> VerificationResult{status, confidence, explanation}`. Can reuse the Executor's context or be a fresh call — if fresh, this is a natural point to allow a different (possibly cheaper) model for the judgment call, since verification is a narrower, more classification-like task than open-ended execution planning. Make this a config knob; default to the same model as the Executor.
- Confidence scoring: `VerificationResult.confidence: float`, surfaced distinctly later (reporting/UI) rather than silently reported as binary pass/fail.
- Retry semantics: if a step fails due to a transient issue (timeout, element not yet visible), retry with backoff before marking it failed. Distinguish `status=failed` (assertion genuinely didn't hold) from `status=error` (infra/tooling problem — Playwright timeout, Claude API error) using the existing `TestRunStatus.error` enum value, which is defined but currently underused.
- Run-level wall-clock timeout per test case (e.g. 5 minutes) so a runaway agent loop can't hang a run forever — extends F-005's try/except concern from "don't crash silently" to "don't hang indefinitely."
- `agent_runner.run_test_suite()` writes more granular status transitions to the store as each step completes, not just at test-case boundaries — this is what makes phase 5's streaming meaningfully real-time rather than just a transport wrapper around the same coarse updates.

## Explicitly NOT in scope

- Streaming (still phase 5).
- Full HTML/PDF reporting (phase 6).
- Slack/Jira alerts on failure (backlog).

## Files to create/modify

- New: `agent/verifier.py`.
- `backend/app/models/schemas.py` — add `VerificationResult`; extend `ExecutionStep` with `confidence: Optional[float]`.
- `agent/executor.py` — call `verifier.evaluate()` for assertion-type steps instead of inline judgment.
- `agent/runner.py` — add retry loop + run-level timeout wrapper.
- `backend/app/services/agent_runner.py` — more granular `store.update_test_run()` calls per step.
- `backend/tests/test_verifier.py` — mock Claude, test confidence/status mapping.
- `backend/tests/test_reliability.py` — simulate a hung step (mock a Playwright call that never resolves) and assert the timeout fires; simulate a transient failure and assert retry-then-succeed.

## Verification

- `pytest`:
  - Retry logic: fails twice, succeeds on 3rd attempt → final status `passed` with a logged retry count.
  - Timeout logic: mock a long-running step → run reaches `status=error` within the timeout window, not stuck at `running`.
  - Verifier confidence scoring against a few canned DOM-state/expected-outcome pairs.
- Manual: intentionally break a test case (point it at a selector that will legitimately fail) and confirm it surfaces as `failed` with a clear verifier explanation, vs. intentionally cause an infra error (kill network mid-run) and confirm it surfaces as `error`, not a silent hang.

## Sizing

M (1–1.5 weeks).

## Status: Code complete, live verification pending an API key

All scope above is built:

- `agent/verifier.py::evaluate()` — one structured-output call returning `VerificationResult{status, confidence, explanation}`. Uses `VERIFIER_MODEL` (config knob in `app/config.py`, defaults to the same model as the Executor).
- `ExecutionStep.confidence` added; the Executor patches it onto the assertion/observation step whose DOM read the verifier judged against.
- `agent/executor.py`: assertion-type steps (or any step with `expected_outcome`) now call `verifier.evaluate()` against the real DOM state captured by `assert_condition`/`read_page` (via `ToolContext.last_snapshot`) instead of parsing the Executor's own RESULT: PASS/FAILED text. The marker convention is kept only as a fallback for the (rare) case where no DOM state was captured.
- Retry-with-backoff: a tool-loop exception retries up to `MAX_STEP_RETRIES=2` times (1s/2s backoff) before being treated as exhausted.
- `status=FAILED` (assertion genuinely didn't hold) vs `status=ERROR` (infra/tooling problem — exhausted retries, verifier call failure, Planner/Executor API failure, run-level timeout) are now distinct `TestStepResult.status` values; the frontend's existing `step.status === "OK"` check and CSS fallback handle the new value with no contract break (`run-step--error` styling added for clarity).
- Run-level wall-clock timeout (`TEST_CASE_TIMEOUT_SECONDS`, default 300s) wraps each test case via `asyncio.wait_for()` in `agent/runner.py`; a hang surfaces as one `ERROR` step within the timeout window instead of hanging the run.
- `agent_runner.py::_run_test_suite_body()` now takes an `on_step` callback threaded through `run_test_case()` → `run_executor()`, pushing `store.update_test_run()` after every individual step (not just at test-case boundaries) — the exact mechanism phase 5's streaming will wrap in a transport layer.
- 18 new tests (`test_verifier.py`, `test_reliability.py`), plus updates to existing Phase 2/3 tests whose fakes needed the new `on_step` parameter or updated status expectations — all passing, no live API calls. Full suite: 66 passed, 1 skipped (the Phase 3 live-API integration test, still gated on `ANTHROPIC_API_KEY`).

**What's not yet done:** same gap as phase 3 — no `ANTHROPIC_API_KEY` was available in this environment, so the verifier's real judgment quality (confidence calibration, whether it's meaningfully more reliable than the old text-marker approach) hasn't been observed against a live Claude call or real staging pages yet. Once a key is available, the phase's own manual verification steps (intentionally-broken selector → `FAILED` with a clear verifier explanation; intentionally-killed network → `ERROR`, not a hang) are the natural first real-world check.
