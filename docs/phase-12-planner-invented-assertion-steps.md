# Phase 12 — Planner no longer invents unrequested assertion steps

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context. Directly
follows phase-11 (same test case, `AD_LG_04`, hit a different bug
immediately after phase-11's fixes landed).

## Goal

Live evidence, right after phase-11 shipped: `AD_LG_04` failed again,
but with a different shape — the Planner produced a plan with an extra
step, "Wait for the email and password fields to be visible on the
login page," that `AD_LG_04.yaml`'s description never asks for at all
(it only says navigate → fill Email → fill Password → click Sign In →
verify Dashboard). That invented step then failed on a
self-contradicting verifier verdict (confidence 0.50: the explanation
quoted the exact "Email / Username *" / "Password *" labels that
satisfy "fields are visible," but still returned `status=failed`).

## Root cause

`agent/planner.py`'s `PLANNER_SYSTEM_PROMPT` contained: "If a step
needs to confirm the login page loaded, phrase the assertion around
what's visibly on screen (e.g. 'the email and password fields are
visible')..." — this was written to redirect an *already-planned*
"confirm the login page" assertion away from a URL check and toward a
content check. It was not written to be conditional on the test case
actually asking for that confirmation, and the model applied it
proactively to a login-flow case that never asked to confirm the
login page loaded at all — echoing the prompt's own example text
almost verbatim as an invented step. The same category of Planner
step-decomposition variance flagged (and explicitly deferred) in
phase-10's "Explicitly NOT in scope" section, now hit again on a
different case.

## Fix

Two changes to `PLANNER_SYSTEM_PROMPT` in `agent/planner.py`:

1. Made the login-page-confirmation guidance explicitly conditional:
   "IF the test case's own description explicitly asks you to confirm
   the login page loaded (and only then — do not add this on your own
   for a case that just goes straight from navigating to filling in
   fields)..."
2. Added a new, general rule: "Do not invent assertion, wait, or
   'confirm X is visible/ready' steps that the test case's own
   description doesn't ask for, even if they seem like reasonable
   intermediate checks — extra steps are extra chances to fail on
   something the test was never actually trying to verify."

Rule 2 is deliberately general (not scoped to the login-page example)
since the underlying failure mode — the Planner padding a plan with
well-intentioned but unrequested checks — isn't specific to login
flows.

## Verification

- Full suite (`pytest --deselect tests/test_agent_runner_integration.py
  --timeout=60`) — 172 passed, no regressions.
- Backend restarted, confirmed serving the updated Planner prompt.
- Live: `AD_LG_04` run 4 times end-to-end against the real staging
  site. All 4 plans came back as exactly the 5 intended steps (no
  invented visibility-check step in any of them) — confirms the
  over-application bug is fixed.

## An interesting live side-effect: flakiness-gated voting caught itself working

Run 1 of the 4 still failed — not from an invented step this time, but
from the verifier's own per-call judgment inconsistency on the
(correct, description-driven) final assertion: its explanation quoted
"paragraphs with titles 'Active Line Items', 'Pending Approvals', and
'Completed Campaigns'" — which is exactly the pass condition — yet
returned `status=failed`. Runs 2-4 of the identical assertion all
passed correctly. This is precisely the per-call non-determinism
majority voting (phase 9) exists to catch.

Checking `backend/data/flakiness_history.json` after the 4 runs:
```json
{"AD_LG_04": 1}
```
One real tie-break was recorded for `AD_LG_04` — the flakiness tracker
correctly detected and logged this exact inconsistency during ordinary
use (not a forced/simulated check like phase-9's own live verification
used). `_FLAKY_THRESHOLD=2`, so one more observed disagreement will
flip `AD_LG_04` to `is_known_flaky=True`, after which it votes on
every run rather than only sampling ~20% of the time. This is the
first real (non-forced) evidence of the phase-9 mechanism operating as
designed in ordinary use, not just under a manufactured `_SAMPLE_RATE`
override.

## Files modified

- `agent/planner.py` — `PLANNER_SYSTEM_PROMPT`'s login-page-assertion
  guidance made explicitly conditional; new general rule against
  inventing unrequested assertion/wait steps.

## Sizing

XS (a two-part prompt wording fix; most of the time was live
verification, and observing the flakiness tracker's real response was
a bonus finding, not additional planned work).

## Status: Done

Fix shipped and live-verified 4/4 for the specific invented-step bug.
The verifier misjudgment on run 1 was NOT investigated further per an
earlier explicit decision (see phase-10's "Explicitly NOT in scope"
and the equivalent choice point in this phase's own triage — the
Planner-prompt fix was chosen over digging into the individual
low-confidence misjudgment) — it doesn't need a separate fix because
the flakiness-gated voting system already exists specifically for
this pattern and is now visibly tracking `AD_LG_04` toward the
known-flaky threshold on its own, in ordinary use.
