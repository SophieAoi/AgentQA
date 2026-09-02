# Phase 11 — Compound-step fill bug + Dashboard URL over-fixation fix

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context.

## Goal

`AD_LG_04` (login with valid email & valid password) started failing
live with no code changes to the test suite in between — a real
regression, not verifier non-determinism (majority voting, phase 9,
wasn't the issue here; the run was consistently wrong for a structural
reason, not flip-flopping). Root-caused live, in order, to two
distinct bugs stacked on top of each other.

## Bug 1 — compound planned steps only got their first action executed

Live evidence: a planned step "Fill the Email field with
`{{VALID_EMAIL}}` and the Password field with `{{VALID_PASSWORD}}`"
produced exactly one `fill()` tool call (the email) in the trace, then
the Executor moved straight to reporting `RESULT: PASS` and on to the
next step — Sign In was clicked with the password field still empty,
so the site correctly stayed on the login page.

Root cause: `EXECUTOR_SYSTEM_PROMPT` in `agent/executor.py` contained
"Once a tool call succeeds, the action it performed is done — do not
call the same tool again with the same or similar arguments to
'confirm' it... A single successful call is enough; immediately move
on to reporting the result." This was written to stop the model from
redundantly re-confirming a *single already-completed* action, but its
wording ("a single successful call is enough") is naturally read as
"stop calling this tool after one success" — which is exactly wrong
when a step legitimately bundles more than one distinct action (two
different fields, or "fill the Name, select the Category, click
Save"). The model picked the more restrictive reading and silently
dropped the password fill.

Fix: reworded the instruction to explicitly distinguish "don't repeat
the SAME action with the SAME arguments" from "a step with multiple
described actions needs multiple tool calls, one per action" —
spelling out the login example directly since it's the exact failure
mode observed.

## Bug 2 — verifier over-indexed on an unstated URL path

Live evidence, after fixing Bug 1: login now genuinely succeeded (the
page content was confirmed live to show "Active Line Items", "Pending
Approvals", "Completed Campaigns" — the real Dashboard), but the step
still failed. The verifier's own explanation: "The current URL...
does not contain '/dashboard'."

Root cause: `AD_LG_04.yaml`'s description said "redirected to the
Dashboard" without specifying whether that means URL or content — and
confirmed live, the real app doesn't use a `/dashboard` path at all;
post-login the URL is just the base URL with the Dashboard rendered at
the root. The model filled in the gap by assuming a URL-path check,
which happens to always fail against this app's real routing.

Fix: rewrote the description to state explicitly that this is a
content check (naming the real, live-confirmed heading/card text) and
that the real post-login URL has no `/dashboard` segment — matching
the project's established pattern of removing model guesswork about
unstated app structure rather than hoping the model infers it
correctly each time (see phase-04's "Campaign vs. deal" fix and
phase-10's Line Item form fixes for the same category of problem).

## Files modified

- `agent/executor.py` — `EXECUTOR_SYSTEM_PROMPT`'s "don't repeat a
  successful tool call" clause reworded to require one tool call per
  distinct described action, with the login case as a worked example.
- `agent/test_cases/AD_LG_04.yaml` — description rewritten to specify
  a content-based Dashboard check and state the real (path-less)
  post-login URL.

## Verification

- Full suite (`pytest --deselect tests/test_agent_runner_integration.py
  --timeout=60`) — 172 passed, no regressions from the prompt/YAML
  changes.
- Backend restarted, confirmed serving the updated prompt.
- Live: `AD_LG_04` run 3 times end-to-end against the real staging
  site after both fixes — 3/3 passed, every step OK each time
  (Navigate → Fill Email → Fill Password → Click Sign In → Dashboard
  content checks), no flakiness observed across the 3 runs.

## Sizing

XS–S (root cause was a single misleading sentence in the Executor's
system prompt, benefiting every multi-action test step, not just this
one case; the second bug was a single test-case wording fix).

## Status: Done

Both bugs fixed and live-verified 3/3. Bug 1's fix is systemic (the
Executor prompt), so it should also un-mask/improve reliability for
any other test case whose planned steps bundle more than one action —
not something this phase went and re-tested against every such case,
but worth keeping in mind if similar step-truncation symptoms show up
elsewhere.
