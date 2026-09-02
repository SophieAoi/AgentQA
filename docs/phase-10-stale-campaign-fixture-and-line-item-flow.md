# Phase 10 — Stale campaign fixture + Line Item creation flow fix

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context, and
`docs/AgentQA_Problems_and_Solutions_Plan.docx` (Problem 4 — stale/
inaccurate test case content) for the original problem statement this
phase resolves a concrete instance of.

## Goal

`INFLUENCE_TEST_CAMPAIGN_ID` has existed in `app/config.py` since early
in the project (comment: "the campaign new Line Items get created
under during test runs") but was never actually wired into any test
case or the Executor — cases needing "an existing deal" were instead
told to search the Campaigns list or create a throwaway one, which is
slower and less deterministic than pointing at a known-good fixture.
Wiring it up surfaced two further problems, both fixed in this phase:
the configured ID was itself stale (pointed at a deleted deal), and the
one real test case exercising deal/Line Item creation
(`Ads_DL_LT_25`) had a description too vague to reliably drive the
real form, which turns out to be a multi-stage conditional form, not a
simple one.

## Scope — fixed

**Stale fixture ID.** `INFLUENCE_TEST_CAMPAIGN_ID` in `.env` pointed to
`9a551523-b7eb-48de-a2d3-a84f548c1956` — confirmed live, this 404s on
staging (the deal was since deleted). Replaced with
`b8196f5e-9eb2-42d7-ae98-7012baaf09cb`, confirmed live to be a real,
current deal ("QA-DIRECT-TEST-0820", status Draft) reachable at
`{INFLUENCE_BASE_URL}/deals/{id}/line-items`.

**Wiring into the Executor.** Same pattern as `INFLUENCE_BASE_URL` and
the "Campaign = deal" wording fix (both in
`EXECUTOR_SYSTEM_PROMPT` — see phase-03/phase-04): a new
`_KNOWN_DEAL_CLAUSE` in `agent/executor.py`, included in the system
prompt only when `INFLUENCE_TEST_CAMPAIGN_ID` is set, instructing the
model to navigate directly to the known-good fixture deal when a step
needs "an existing deal/campaign" without naming one, rather than
searching the list or creating a throwaway one. Benefits every case
that references "an existing deal," not just `Ads_DL_LT_25`.

**Line Item creation form, fully mapped live.** The real form
(`New Line Item` on a deal's Line Items page) is a multi-stage
conditional form, confirmed by direct interaction (not guessed):

1. Line Item Name (plain text).
2. Media Owner (dropdown) — **critical finding**: the form's default
   Media Owner ("Abc Cooking Studio") has zero inventory/screens
   configured on this staging environment for any Creative
   Type/Inventory Type combination (confirmed by testing all 6
   combinations — all showed "0 selected of 0 recommended"). Switching
   Media Owner to "Jeki" (the test account's own company) unlocked 337
   real, selectable screens. This is a genuine staging-data gap, not
   an agent/selector bug — the fix is test-case wording, not code.
3. Creative Type (dropdown; "Display" used).
4. Inventory Type (dropdown, appears after Media Owner + Creative Type
   are set; "Digital" used).
5. Flight Dates — a click-driven date-range calendar picker, not a
   text input. Confirmed live: selecting a range via the "Next 7 days"
   quick-select does **not** commit the dates by itself; a
   "Cancel" / "Apply" button pair appears once a range is picked, and
   the field stays unset until "Apply" is clicked. This was the
   original failure mode reported at the start of this phase (agent
   selected dates but never clicked Apply, so the form silently
   rejected submission with "Start date is required" / "End date is
   required" despite the calendar visually showing a selection).
6. "Browse All" (inventory/screen picker) — disabled until steps 1-5
   are complete (confirmed live: tooltip reads "Complete the required
   fields to browse inventory" on the disabled state). Opens a panel
   showing 0 or more matching screens depending on Media Owner/
   Creative Type/Inventory Type; "Select All" + "Apply Selection"
   commits a selection.
7. Only then does "Create Line Item" — clickable throughout, but
   **not disabled when required fields are missing** — actually
   submit; an incomplete form re-renders the same page with inline
   validation errors instead of navigating away, which is the correct
   signal for "not created," confirmed against the verifier's own
   judgment on an incomplete-form submission.

`agent/test_cases/Ads_DL_LT_25.yaml`'s description was rewritten to
spell out this exact sequence, the Media Owner trap, and the
Apply-button requirement — same precision-over-vagueness pattern as
`INF_PG_01`'s existing campaign-creation field guidance.

## Explicitly NOT in scope

- Fixing Planner step-decomposition variance (observed live: one run
  of the rewritten `Ads_DL_LT_25` had the Planner insert a spurious
  "is the Create Line Item button enabled" pre-check before any field
  was filled, which fails because there's nothing meaningful to check
  yet, cascading to skip the rest of the run). This is the same
  general per-call model non-determinism problem majority voting
  (phase 9) targets at the verifier layer — the Planner doesn't have
  an equivalent mechanism yet, and building one is out of scope here.
  Tracked as a general instance of Problem 1, not a defect specific to
  this test case.
- A dedicated date-range-picker tool in
  `agent/tools/playwright_tools.py`. The existing generic `click()`
  tool proved sufficient once the test case description explained the
  picker's actual interaction model (open → quick-select → Apply) —
  no new tool needed for this case.
- Auditing every other test case that references "campaign"/"deal" for
  similar vagueness. Only `Ads_DL_LT_25` (the one case that actually
  exercises Line Item creation) was rewritten; the ~40 other cases
  referencing campaigns/deals do so for simpler read/navigate/assert
  purposes not affected by this form's complexity.

## Files modified

- `.env` — `INFLUENCE_TEST_CAMPAIGN_ID` updated to a live-verified real
  deal ID.
- `agent/executor.py` — `_KNOWN_DEAL_CLAUSE` added; imports
  `INFLUENCE_TEST_CAMPAIGN_ID`.
- `agent/test_cases/Ads_DL_LT_25.yaml` — description rewritten with
  the full, live-confirmed field sequence.

## Verification

- Full suite (`pytest --deselect tests/test_agent_runner_integration.py
  --timeout=60`) — 172 passed, unaffected (this phase touches config
  and prompt text, not tested code paths directly).
- Backend restarted; confirmed serving the updated `.env` and executor
  prompt.
- Live, direct browser scripting (not the agent) confirmed each stage
  of the form individually: the stale ID's 404, the real deal's
  reachability, all 6 Creative-Type/Inventory-Type combinations having
  zero inventory under the default Media Owner, 337 screens becoming
  available under "Jeki," and a full manual run creating a real Line
  Item (deal's "Total Line Items" count increased from 1 to 2 across
  two probe runs) with "Activate Campaign" appearing afterward.
- Live agent run: the "open an existing deal" step now passes via
  direct navigation to the fixture (previously required a slower
  search-or-create sequence) — confirmed across two separate live
  agent runs. A full run reached through the Flight Dates Apply step
  cleanly (11 consecutive OK steps) before hitting Planner variance
  unrelated to the description's accuracy (see Explicitly NOT in
  scope).

## Sizing

S–M (~1 session; most of the time was live form-mapping, not code).

## Status: Done

The stale-fixture and missing-wiring problems are fully fixed and
live-verified. The Line Item form's real requirements are now fully
documented in the test case, confirmed correct via direct browser
control. `Ads_DL_LT_25` has not yet been observed to pass **as one
complete agent run start-to-finish** — the closest live agent run
reached 11 consecutive correct steps before an unrelated Planner
pre-check invented itself and cascaded a skip. Given majority voting
(phase 9) already exists as the general mitigation pattern for
per-call model non-determinism, extending an equivalent safeguard to
Planner-level step decomposition is the natural next step if this
turns out to recur across other multi-stage-form test cases — not
pursued further in this phase per explicit scoping decision.
