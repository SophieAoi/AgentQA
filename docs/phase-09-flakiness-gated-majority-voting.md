# Phase 9 — Flakiness-gated majority-vote verification

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context, and
`docs/AgentQA_Problems_and_Solutions_Plan.docx` (Problem 1) for the
original problem statement and the two decision points this phase
resolves.

## Goal

Fix genuine per-call non-determinism in `agent/verifier.py`'s model
judgments (confirmed live, repeatedly: the same real DOM state and
expected outcome, judged by the same model on separate calls,
occasionally disagrees with itself — e.g. AD_LG_01 flip-flopping
pass/fail across runs with zero code changes in between, while a
same-input 5x retest came back 5/5 correct) — without paying the cost
of voting on every single assertion in every run, which is what the
first version of this fix did and which measurably slowed every test
case down.

## Scope — build

Delivered in three sub-phases, each shipped and verified before the
next started:

**9a — Unconditional majority vote.** `_evaluate_single()` in
`agent/verifier.py` calls the model twice on the same DOM
state/expected-outcome pair; if both agree, that's the answer; if they
disagree, a third call breaks the tie by majority. Applied to every
assertion, every run.

**9b — Flakiness-gated voting.** New `agent/flakiness_tracker.py`:
persisted, per-test-case count of how many times that case's votes
have actually disagreed (`record_tie_break`, `is_known_flaky` at
`_FLAKY_THRESHOLD=2`). `_evaluate_single()` only pays the 2-3x call
cost when the case is already known-flaky — every other case takes the
original single-call fast path, recovering the speed lost in 9a for
the large majority of cases that never actually show inconsistency.

**9c — Sampling to bootstrap discovery.** Gating purely on
`is_known_flaky()` has a cold-start gap: a case that's never been
voted on can never accumulate its first tie-break, so it could never
become "known flaky" in the first place. Added
`should_sample_vote()` — an independent ~20% (`_SAMPLE_RATE`) random
check that also triggers voting for a not-yet-flagged case, so latent
flakiness still gets discovered over the course of normal runs instead
of only ever being checked for once and never again.

Persistence is a small local JSON file
(`backend/data/flakiness_history.json`), not the existing in-memory
`StoreProtocol`, because that store resets on every backend restart —
flakiness history needs to survive restarts to accumulate real signal
over time. The directory is gitignored (machine-local runtime state,
not something to share across clones).

## Explicitly NOT in scope

- A full pass/fail history or results dashboard (`flakiness_tracker.py`
  only answers "has this case's votes ever disagreed," not a general
  results store — see Problem 2 in the Problems & Solutions doc for
  the larger dashboard idea).
- Any change to the Executor or Planner — this phase is entirely
  inside `agent/verifier.py`'s judgment step and its one call site in
  `agent/executor.py::run_executor()`.
- Cross-model verification (running the vote against a second, distinct
  model rather than the same model twice) — noted as a possible future
  escalation if same-model voting turns out insufficient, not built now.

## Files created/modified

- New: `agent/flakiness_tracker.py` — `should_sample_vote()`,
  `is_known_flaky()`, `record_tie_break()`, `get_tie_break_count()`,
  `reset()`.
- `agent/verifier.py` — `_evaluate_single_call()` (the original,
  un-wrapped one-shot call) + `_evaluate_single()` (the new voting
  wrapper, gated by `is_known_flaky() or should_sample_vote()`);
  `evaluate()` threads `test_case_id` through to all three call sites.
- `agent/executor.py` — `run_executor()`'s `verify()` call now passes
  `test_case_id=test_case_id`.
- `.gitignore` — `backend/data/` added.
- New: `backend/tests/test_flakiness_tracker.py` — 9 tests: new-case
  defaults, single tie-break below threshold, threshold crossing,
  per-case isolation, file persistence across calls, `reset()` (single
  case and global), `None` test_case_id degrading safely,
  `should_sample_vote()` respecting `_SAMPLE_RATE`, corrupt state file
  recovering instead of crashing.
- `backend/tests/test_verifier.py` — every test explicitly patches
  `should_sample_vote` (and, for one test, `is_known_flaky`) to force
  the vote/no-vote decision deterministically; added
  `test_evaluate_single_returns_the_immediate_verdict_when_both_votes_agree`,
  `test_evaluate_single_breaks_a_tie_with_a_third_vote`,
  `test_evaluate_single_tiebreaker_can_also_confirm_a_failure`,
  `test_evaluate_votes_for_a_known_flaky_case_even_without_sampling`
  (the last one proves the `is_known_flaky` OR-branch works
  independently of the sampling branch — the two triggers are tested
  in isolation from each other, not just together).

## Verification

- `pytest backend/tests/test_flakiness_tracker.py
  backend/tests/test_verifier.py` — 25/25 passed.
- Full suite (`pytest --deselect tests/test_agent_runner_integration.py
  --timeout=60`) — 172 passed, 0 regressions (up from 161 pre-feature).
- Backend restarted against the new code;
  `GET /docs` returns 200.
- Live check: ran `AD_LG_01` three times end-to-end against the real
  staging site with `_SAMPLE_RATE` forced to 1.0 (voting guaranteed on
  every assertion, without touching the real 0.2 default on disk —
  the override was a module-attribute patch in a throwaway script's
  own process, not a source edit). All three runs passed cleanly; the
  verifier's two votes agreed each time (tie_break_count stayed 0,
  `is_known_flaky` stayed `False`) — confirms the voting mechanism
  itself executes correctly end-to-end against the real model and
  real site, consistent with the model's real per-call accuracy being
  high (~90%+) and voting existing to catch the rare miss, not
  because misses are common.

## Sizing

S–M (~1 session, delivered in the three sub-phases above).

## Status: Done

All scope built, unit-tested, and live-verified. What's *not* yet been
observed live is an actual tie-break firing and a case crossing
`_FLAKY_THRESHOLD` into `is_known_flaky=True` under the real 20%
sample rate during ordinary use — that requires either genuine bad
luck on a real run or another forced-rate live check, and isn't
blocking: the tie-break/threshold logic itself has direct unit
coverage (`test_evaluate_single_breaks_a_tie_with_a_third_vote`,
`test_repeated_tie_breaks_mark_a_case_flaky`), and the live check above
confirmed the surrounding gate, vote execution, and persistence layer
all work correctly against the real model. Natural next observation
point: watch `backend/data/flakiness_history.json` over the course of
normal day-to-day test runs rather than manufacturing it.
