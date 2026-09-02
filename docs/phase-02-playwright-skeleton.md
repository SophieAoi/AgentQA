# Phase 2 — Playwright skeleton + real browser session (no LLM yet)

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context and design rationale.

## Goal

Prove Playwright automation works end-to-end against a real page — login, navigation, a fixed action sequence — with **zero LLM involvement**, so phase 3 only has to wire "LLM decides the action" on top of browser plumbing that's already known-good.

## Scope — build

- New top-level `agent/` package (the paths `agent/browser/login.py` and `agent/test_cases/` are already referenced in `agent_runner.py`'s existing docstring — this phase is where they actually get built):
  - `agent/browser/session.py` — Playwright context/page lifecycle: launch, navigate, close, screenshot-on-error.
  - `agent/browser/login.py` — the OAuth/credential-based login flow `SiteViewer.jsx`'s comment already references, parameterized by env-provided credentials (never hardcoded).
- `agent/test_cases/` — one file per test case (`TC-001.yaml` etc.), fields: `id`, `title`, `description` (natural-language steps), `preconditions` (e.g. "requires login"). Migrate the 4 existing hardcoded cases out of `TestRunnerPanel.jsx`'s `AVAILABLE_TEST_CASES` and `agent_runner.py`'s `_ACTION_SCRIPTS`.
- `agent/runner.py::run_test_case()` — a **hardcoded, non-LLM** implementation: launches Playwright, logs in if required, runs a fixed sequence of real Playwright calls per test case (literally translating today's fake `_ACTION_SCRIPTS` log lines into real `page.click()` / `page.fill()` calls against the real Influence staging site), captures a screenshot on each step and on failure, returns a step-result list.
- `agent_runner.py`'s `run_test_suite()` now calls the real `agent.runner.run_test_case()` instead of the `asyncio.sleep()` + `random.random()` simulation — this resolves that file's own long-standing TODO.
- New endpoint: `GET /test-cases`, returning parsed `agent/test_cases/` contents (closes `TestRunnerPanel.jsx`'s TODO about loading test cases from a real source).
- Env-based credentials: `INFLUENCE_TEST_USERNAME` / `INFLUENCE_TEST_PASSWORD` (or similar), added to `.env.example`, loaded via phase 1's `backend/app/config.py`.

## Explicitly NOT in scope

- Any LLM call in the execution path — this phase is deliberately LLM-free so Playwright reliability issues are isolated from agent-reasoning issues.
- Self-healing selectors — fixed selectors only (phase 3).
- Full `ExecutionStep`/`AgentTrace` models — reuse/extend the existing `TestStepResult` minimally, just enough to carry a real `screenshot_url` per step.

## Files to create/modify

- New: `agent/__init__.py`, `agent/browser/__init__.py`, `agent/browser/session.py`, `agent/browser/login.py`, `agent/runner.py`, `agent/test_cases/TC-001.yaml` … `TC-004.yaml`.
- `backend/app/services/agent_runner.py` — call into `agent.runner.run_test_case()`.
- `backend/app/routers/test_runs.py` — no route-surface change; now backed by real execution.
- New: `backend/app/routers/test_cases.py` — `GET /test-cases`.
- `frontend/src/components/TestRunnerPanel.jsx` — fetch test cases from `GET /test-cases` instead of the hardcoded array.
- `frontend/src/api.js` — add `getTestCases()`.
- `backend/requirements.txt` — add `playwright==`; document the `playwright install` post-install step in the README.
- `backend/tests/test_agent_runner.py` — mock Playwright, or run headless against a small local fixture HTML page checked into the repo (`backend/tests/fixtures/`) so CI doesn't depend on the real staging site's uptime/credentials.

## Verification

- Manual: trigger a real run from the UI (headed mode locally for debugging), confirm the browser actually navigates/fills/clicks, screenshots are saved and served, login works with real staging credentials.
- `pytest` against the local fixture page — control-flow coverage without depending on the real staging environment.
- Confirm `GET /test-runs/{id}` still returns the same `TestRunDetail` shape the frontend already renders — this phase should be invisible to the frontend polling contract except that the data is now real.

## Sizing

M (4–6 days). Highest real-world-flakiness risk in the early phases — real site, real login/OAuth timing. Budget extra time for selector/timing debugging against the actual staging app.

## Status: Complete, with one documented environment gap

All scope above is built and verified against the real staging site through the actual running app (not just scratch scripts):

- **TC-002** (blank Line Item Name) — **passes for real.** App correctly rejects with `"Line Item Name"` flagged.
- **TC-003** (negative Rate) — **passes for real.** App correctly rejects with `"Rate"` flagged.
- **TC-001** (create a valid Guaranteed deal) and **TC-004** (duplicate name) — **fail, honestly and correctly.** Both reach the real "Create Line Item" submit and get rejected with `selectedInventoryIds` flagged as missing. This traces to a **real data gap in this staging environment**, not a bug in the automation or the app: the only two Media Owners that exist (`Jeki`, `マレーシア`) were checked — `Jeki` returns **zero results** from both the "Browse All" inventory browser and the AI Smart Recommendation engine (no filters applied, so this isn't a filter-matching issue). No screens/inventory are currently registered under either Media Owner in this environment.
- This is the correct, desired behavior for a QA agent to exhibit: report the real blocker with a specific, actionable detail message rather than silently passing or crashing. See `agent/runner.py`'s module docstring and the `TestStepResult.detail` text on failure for the exact wording surfaced to the user.

**To unblock TC-001/TC-004 fully:** bookable Digital inventory needs to be added under a real Media Owner in the staging environment (or a different campaign/media owner with existing inventory needs to be identified) — this is an environment setup task, not a code task. Once available, update `MEDIA_OWNER_NAME` in `backend/agent/runner.py` (and `INFLUENCE_TEST_CAMPAIGN_ID` in `.env` if a different campaign is needed) — no other changes required.

The real "Create Line Item" form turned out to be substantially deeper than the original placeholder `_ACTION_SCRIPTS` assumed: Line Item Name → Purchase Type → DSP → Buyer/Seat → Rate → Media Owner → Flight Dates → Threshold Count Per Day → inventory selection, each step discovered live against the real site (see chat history for the field-by-field exploration). All real values used (DSP `Moving Audiences Xchange`, Buyer/Seat `12weqwefwef (123123)`, Media Owner `Jeki`) were confirmed with the user before any real data was submitted.
