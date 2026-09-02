# Phase 15 — Create/edit/delete test cases from the UI

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context.

## Goal

Test case YAML files (`agent/test_cases/*.yaml`) could previously only
be created, edited, or deleted by hand-editing files directly. A
separate, static "Test Case Tracker" artifact was published earlier
this session as a checklist for going through the 256 real cases
one-by-one — but a published web page has no filesystem access
whatsoever (a hard browser security boundary, not a missing feature),
so it could never be the thing that actually edits
`agent/test_cases/`. The real fix is a proper CRUD UI in the existing
frontend/backend, writing straight through to those same files — so a
change made in the browser is immediately what the next test run,
`GET /test-cases`, or a hand-opened YAML file all see.

## Scope — build

**Backend** (`agent/runner.py`, `app/routers/test_cases.py`,
`app/models/schemas.py`):
- `save_test_case(id, title, description, suite, preconditions,
  essential, *, overwrite)` — writes one `agent/test_cases/{id}.yaml`.
  `overwrite=False` (create) fails if the id already exists;
  `overwrite=True` (edit) fails if it does NOT exist yet. Renaming a
  case's id isn't supported by this function — that's delete + create
  under a different name, kept as two explicit operations rather than
  overloaded into one.
- `delete_test_case(id)` — removes the file.
- `_test_case_path(id)` — validates the id against
  `^[A-Za-z0-9_-]+$` before ever building a path from it, since the id
  ultimately comes from an HTTP request body; this is the actual
  path-traversal defense (a crafted id like `../../etc/passwd` is
  rejected before any file operation).
- A custom YAML dumper (`_FoldedStr` + a registered representer) makes
  a freshly-saved file's `description` field use the same `>` folded
  block-scalar style every hand-written test case file already uses,
  rather than PyYAML's uglier default wrapped-single-line style — a
  saved file is indistinguishable from a hand-written one.
- `POST /test-cases` (201, create), `PUT /test-cases/{id}` (200,
  edit), `DELETE /test-cases/{id}` (204) — all behind the existing
  `get_current_user` auth dependency, same as the read endpoint.
  Validation errors map to 400 (bad id, missing field, duplicate id)
  or 404 (edit/delete of an id that doesn't exist).

**Frontend** (`TestRunnerPanel.jsx`, new `TestCaseEditor.jsx`,
`api.js`, `App.css`):
- `createTestCase`/`updateTestCase`/`deleteTestCase` added to
  `api.js`, following the exact pattern every other endpoint call in
  that file already uses.
- `TestCaseEditor.jsx` — a modal form for both create and edit
  (`existing={null}` vs `existing={testCase}` selects the mode). The
  id field locks once editing (matches the backend's rename
  restriction). Client-side validation mirrors the backend's id regex
  so a typo is caught before a round trip, but the backend remains the
  real authority.
- `TestRunnerPanel.jsx` — each test case row gained hover-revealed
  Edit/Delete buttons (kept out of the row's own `<label>` so clicking
  them doesn't also toggle the run-selection checkbox); a "+ New Test
  Case" button in the bulk-actions bar; delete asks
  `window.confirm()` first and names the exact file being removed.
  The suite list and counts recompute automatically after any
  create/edit/delete via a shared `refetchTestCases()`.

## Explicitly NOT in scope

- Renaming a test case's id through the UI (delete + recreate under a
  new id is the workaround; a dedicated rename operation wasn't
  requested).
- Bulk edit/delete (multi-select + apply to many cases at once).
- Any change to the previously-published static Test Case Tracker
  artifact — it remains a separate, read-only, per-viewer checklist
  for progress-tracking (its checked/unchecked state lives in that
  viewer's browser localStorage), not connected to this new CRUD UI in
  any way. It will drift from the real file list over time as cases
  are added/edited/deleted here; regenerating it is a manual step, not
  automatic.
- Undo/version history for edits or deletes — a saved edit or a
  delete is immediate and, for delete, irreversible from the UI itself
  (though the file's still just a file — normal filesystem/backup
  recovery still applies outside this feature).

## Files created/modified

- `agent/runner.py` — `save_test_case()`, `delete_test_case()`,
  `_test_case_path()`, `TestCaseValidationError`, `_FoldedStr` +
  representer.
- `app/models/schemas.py` — `TestCaseWrite`, `TestCaseCreate`.
- `app/routers/test_cases.py` — `POST`/`PUT`/`DELETE` handlers.
- `tests/test_test_cases.py` — 16 new tests (write-function unit tests
  isolated via a `tmp_path` override of `TEST_CASES_DIR`, plus
  router-level tests for all three new endpoints including auth and
  404/400 cases); one existing count assertion corrected (`Login`
  suite: 15 → 14, reflecting `Ads_Login_N03`'s deletion as a duplicate
  of `AD_LG_05` earlier this session).
- `frontend/src/api.js` — `createTestCase`, `updateTestCase`,
  `deleteTestCase`.
- `frontend/src/components/TestCaseEditor.jsx` — new.
- `frontend/src/components/TestRunnerPanel.jsx` — editor state,
  `refetchTestCases()`, `handleDelete()`, per-row action buttons, "+
  New Test Case" button.
- `frontend/src/App.css` — row action-button styles, modal styles,
  `.bulk-action-button--primary` variant.

## Verification

- Backend: full suite (`pytest --deselect
  tests/test_agent_runner_integration.py --timeout=60`) — 195 passed
  (179 prior + 16 new), no regressions.
- Frontend: `npm test` (vitest) — 3 passed, no regressions; dev server
  compiles both new/changed files with no errors.
- Live, end-to-end through the real browser UI (not just unit tests):
  a Playwright-driven script logged into the app, opened "+ New Test
  Case," filled the form, and created `TC_UI_SMOKE_TEST` — confirmed
  the file landed at `agent/test_cases/TC_UI_SMOKE_TEST.yaml` in exact
  house style. Then edited its title through the UI (confirmed the
  change appeared in the row) and deleted it through the UI (confirmed
  `window.confirm` fires, the row disappears, and the file is gone
  from disk). Suite/runnable counts in the header updated correctly
  after each operation without a page reload.

## Sizing

M (a full CRUD slice across backend, schema, and frontend, plus a
real live UI verification pass — not just code review).

## Status: Done

All three operations (create, edit, delete) are live-verified working
end-to-end through the actual browser UI, writing directly to
`agent/test_cases/*.yaml`. This directly satisfies the original
request: "changes made in the test case tracker reflected here" — by
building the real editor into the app that already has filesystem
access, rather than trying to give a published static page a
capability the browser sandbox fundamentally does not allow.
