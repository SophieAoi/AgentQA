# Phase 14 — Blank-field no-op steps + persistent merged-text verifier bias

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context. Triggered by
`AD_LG_05` (empty username/password mandatory-field check), the
sibling case to `AD_LG_04` which phases 11-13 already worked through.

## Goal

`AD_LG_05` failed live with the Planner generating "Leave the Email
field blank" / "Leave the Password field blank" as `action` steps —
which the Executor correctly refused to fabricate a fake tool call
for, and correctly marked FAILED ("the model described this action in
text but never issued a real tool call"). Investigating this surfaced
a second, harder problem: even once that was fixed, the verifier
persistently misjudged whether a validation message was present,
tracing back to the same aria-snapshot text-merging artifact noted in
passing during phase-12/13's investigations but not yet addressed
directly.

## Bug 1 — Planner should never plan a step for "leave X blank" (fixed)

There is no tool call for doing nothing — a field that's simply never
mentioned in a fill step is already blank; that's the correct
representation of "leave it blank." `agent/planner.py`'s
`PLANNER_SYSTEM_PROMPT` gained an explicit rule: never plan a step for
"leave field X blank" / "don't fill in Y" — omit it entirely.

**Verified live**: after the fix, `AD_LG_05`'s plan consistently came
back as exactly 3 steps (navigate, click Sign In, one assertion) with
no blank-field steps in any of 9+ live runs performed while
investigating Bug 2 below.

## Bug 2 — real site validation behavior didn't match the test case (fixed)

Also confirmed live while investigating: with both Email and Password
blank, the site's form validates **one field at a time, top to
bottom** — only "Email / Username is required" appears; there is no
simultaneous Password message (confirmed by also checking the
Email-filled/Password-blank state, which correctly shows "Password is
required" instead). `AD_LG_05.yaml`'s original description expected
both fields' validation to appear together, which the real UI never
does. Rewritten to expect exactly what the real form does, and
simplified to a single, well-anchored assertion (checking for the
literal substring "Email / Username is required") after an
intermediate two-assertion version ("...and the login is blocked")
proved to have its own new ambiguity (see Bug 3).

## Bug 3 — verifier's merged-text bias (fixed — see Follow-up below for the resolution)

Playwright's `aria_snapshot()` (used by `_page_snapshot()` in
`agent/tools/playwright_tools.py`) concatenates adjacent unlabeled
text nodes onto a single `text:` line with only a space between them,
even when they come from unrelated parts of the real page. Confirmed
live: the validation message and the Password field's label collapse
into one line: `text: Email / Username is required Password *`. This
is a real accessibility-tree serialization artifact (Playwright's,
not this codebase's), not a Verifier reasoning bug in the sense of
"the text isn't there" — the text genuinely is there, verbatim, as a
substring.

Three consecutive attempts to fix this via `VERIFIER_SYSTEM_PROMPT`
wording (`agent/verifier.py`), each tested against 3-5 live runs:

1. First attempt (a general statement that adjacent text nodes can
   merge without a real visual boundary) had no measurable effect —
   still ~50-75% failure rate on this exact assertion across live
   runs.
2. Second attempt, more explicit ("if the phrase appears anywhere as
   a literal substring, merged or not, that counts as present; never
   fail for text looking merged") made things measurably **worse**
   (1/5 passed) — the model started actively citing "merged with
   other text" and "not in a separate line" as its stated FAILURE
   reasoning, i.e. it had picked up the vocabulary from the prompt
   and was now using it as a reason to fail rather than a reason to
   still pass, the opposite of intent.
3. A root-cause fix was also attempted and explicitly reverted: a
   regex-based post-processor
   (`_split_merged_text_lines()`/`_MERGED_LABEL_SPLIT_RE`, briefly
   added to `agent/tools/playwright_tools.py`) that tried to detect
   and re-split merged `text:` lines by looking for a
   `<Word(s)> *`-shaped required-field label appearing mid-line. Unit
   testing this against real captured snapshot text before shipping
   it found two correctness bugs on the first try: it incorrectly
   split an *unmerged*, legitimate label ("Email / Username *" alone
   became "Email /" + "Username *"), and it failed to catch a
   different real merge pattern that doesn't end in `*` ("Start date
   is required End date is required"). Given a heuristic that broke
   on its first real test case, this was reverted rather than shipped
   half-working — see git history / this doc for why a future
   attempt at this approach should not restart from the same regex.

**Current status**: the verifier prompt still carries the (only
partially effective) explanation of the merging artifact — kept
because it's not harmful and may still help in adjacent cases, even
though it didn't resolve this one. The actual, intended safety net for
this residual bug is flakiness-gated majority voting (phase 9):
`AD_LG_05` accumulated 1 real tie-break during this phase's live
verification (of `_FLAKY_THRESHOLD=2`) — one more observed disagreement
flips it to `is_known_flaky=True`, after which it always votes, and
since the underlying page state genuinely, consistently contains the
expected text, a 2-of-3 majority vote should reliably resolve to
"passed" even while any single call has an elevated chance of the
"merged text" misjudgment. This was not force-verified to full
resolution in this phase (would require either more live runs to
naturally cross the threshold, or a forced-sample-rate check like
phase-9's own live verification used) — flagged as the natural
follow-up if `AD_LG_05` continues to show up as failing in ordinary
use after crossing threshold.

## Follow-up — the snapshot-splitting fix, done properly

Reported live again after this phase first shipped: `AD_LG_05` still
failed with the exact same symptom ("does not contain a separate line
of text... appears as part of the merged text"). Given three prompt
attempts had already failed and the flakiness-gated-voting fallback
hadn't yet crossed threshold for this run, the regex-based snapshot
fix was revisited — this time validated much more rigorously before
shipping, directly addressing why the first attempt broke.

**What was different this time**: captured real snapshots from four
different real pages (the blank-field login form, the dashboard, the
campaigns list, and the Line Item creation form) up front, and used
them to build a much narrower regex — `_MERGED_REQUIRED_SPLIT_RE` in
`agent/tools/playwright_tools.py` — that only splits a `text:` line
directly after the literal word "required", and only when what
immediately follows also looks like another short requirement/label
phrase (ends in `" *"` or itself contains `"is/are required"`). The
original attempt's regex matched on any `<Word> *` pattern regardless
of context, which is what let it split *inside* a legitimate single
label; anchoring specifically on "required" as the split point removes
that failure mode, since "required" essentially never appears in the
middle of an ordinary field label.

Verified against 7 cases before shipping (now permanent regression
tests in `tests/test_page_snapshot.py`):
- Splits `"Email / Username is required Password *"` → two lines (the
  original bug).
- Splits `"Start date is required End date is required"` → two lines
  (the no-asterisk variant, confirmed live on the Line Item form).
- Does **not** split `"Email / Username *"` alone (the exact case that
  broke the first attempt).
- Does **not** split a label merged with its own helper text
  ("Line Item Name A descriptive name for this line item...").
- Does **not** split ordinary prose containing "required" with nothing
  label-shaped following it.
- Does **not** touch non-`text:` lines (headings, paragraphs) even
  when their content looks similar.
- Splits only the affected line in a realistic multi-line snapshot,
  leaving every other line byte-identical.

**Live result**: `AD_LG_05` re-run 5 times end-to-end against the real
site after the fix — **5/5 passed**, up from roughly 1-in-5 before.
This is the actual root-cause fix; flakiness-gated voting is no longer
load-bearing for this specific case (though it stays on as the general
safety net for whatever the next per-call inconsistency turns out to
be — it did still catch one incidental tie-break during these 5 runs
via ordinary sampling, unrelated to this bug).

## Files modified

- `agent/planner.py` — new rule against planning "leave X blank" steps.
- `agent/test_cases/AD_LG_05.yaml` — description rewritten twice: once
  to match the real one-field-at-a-time validation behavior, once more
  to collapse to a single, unambiguous assertion.
- `agent/verifier.py` — `VERIFIER_SYSTEM_PROMPT` gained a (partially
  effective on its own) explanation of the snapshot text-merging
  artifact; kept as defense-in-depth alongside the code fix below.
- `agent/tools/playwright_tools.py` — a first snapshot post-processing
  fix was added and reverted in this phase; a second, much narrower
  version (`_split_merged_required_text()` /
  `_MERGED_REQUIRED_SPLIT_RE`) was added in the follow-up below and
  shipped after passing 7 cases built from real captured snapshots.
- `tests/test_page_snapshot.py` — 7 new tests covering
  `_split_merged_required_text()` directly (see Follow-up).

## Verification

- Full suite (`pytest --deselect tests/test_agent_runner_integration.py
  --timeout=60`) — 172 passed throughout every change in this phase,
  including after the final revert.
- Live: Bug 1 (blank-field no-op steps) — fully fixed, confirmed
  across 9+ live runs with zero recurrences after the fix.
- Live: Bug 2 (validation-timing mismatch) — confirmed via direct
  browser scripting (not the agent) checking both blank-fields and
  Email-filled/Password-blank states.
- Live: Bug 3 (merged-text bias) — not resolved by three prompt
  attempts or the first, reverted code attempt (documented above so a
  future session doesn't re-try the same wording/regex); resolved by
  the second, narrower code attempt in the Follow-up section — 5/5
  live runs passed after shipping, plus 7 permanent unit tests.

## Sizing

M (two clean wins plus one problem that initially resisted three
prompt attempts and one reverted code attempt, but was ultimately
fixed properly with a more careful second code attempt in the
Follow-up section).

## Status: Done

All three bugs are fixed and live-verified. Bug 3 took the most
iteration: three prompt-wording attempts and one reverted code attempt
before a second, more rigorously-validated code fix
(`_split_merged_required_text()`) resolved it — 5/5 live passes,
up from roughly 1/5 before, plus 7 permanent regression tests. The
lesson carried into that fix: validate a text-processing heuristic
against a range of real captured inputs (including the cases it must
NOT change) before shipping it, not just the one failing case it's
meant to fix — the first attempt skipped that step and broke a
legitimate label on its very first real test.
