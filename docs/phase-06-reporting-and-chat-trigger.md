# Phase 6 — Reporting + chat-driven trigger

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context and design rationale.

## Goal

Turn the structured `ExecutionStep`/`AgentTrace` data into human-consumable reports, and close the loop on the original ask that a user can trigger runs via natural-language chat, not just the checkbox panel.

## Scope — build

- `agent/reporter.py` — **deterministic templating, not an LLM call** (see [BUILD-PLAN.md](BUILD-PLAN.md#why-two-agent-roles-planner--executor-not-five) on why Reporter was never a fifth agent). Takes a completed `TestRunDetail` + its `ExecutionStep`s and renders an HTML report (Jinja2) with pass/fail summary, per-step detail, embedded screenshots, and verifier confidence/explanation where relevant.
- PDF export via Playwright's own print-to-PDF (reuses the existing browser-automation dependency, avoids a second heavyweight PDF library) or `weasyprint` (cleaner HTML/CSS control, one more dependency) — pick one and document the tradeoff in the implementation.
- New endpoints: `GET /test-runs/{run_id}/report` (HTML) and `GET /test-runs/{run_id}/report.pdf`.
- `ChatService` (from phase 1) gains **one** function tool, `start_test_run(test_case_ids: list[str])`, wired through Claude's tool-runner loop — the first real instance of Claude deciding to act from chat, closing the original stub's docstring aspiration ("run TC-001 through TC-005 and tell me about any failures"). Deliberately scoped to exactly one tool.
- Frontend: a "View Report" affordance on completed runs in `TestRunnerPanel`, opening the HTML report in a new tab or triggering the PDF download.

## Explicitly NOT in scope

- Email/Slack delivery of reports (backlog).
- Scheduling/CI triggers (backlog).
- Any chat tool beyond `start_test_run` — no `read_report`, no `cancel_run` yet.

## Files to create/modify

- New: `agent/reporter.py`, `agent/templates/report.html.jinja2`.
- New: `backend/app/routers/reports.py` — `GET /test-runs/{run_id}/report`, `/report.pdf`.
- `backend/app/services/chat_service.py` — add the `start_test_run` tool + tool-runner loop.
- `frontend/src/components/TestRunnerPanel.jsx` — "View Report" affordance.
- `frontend/src/api.js` — `getReportUrl(runId)`.
- `backend/tests/test_reporter.py` — given a canned `TestRunDetail` + `ExecutionStep` list, assert the rendered HTML contains expected content (pass/fail counts, referenced screenshots).
- `backend/tests/test_chat_tool_use.py` — mock Claude's tool-call response, assert `start_test_run` actually calls `store.create_test_run` and schedules the background task, and that the chat reply reflects the triggered run.

## Verification

- `pytest`: reporter renders correctly for both an all-pass and a mixed-failure run; chat tool-use test confirms a natural-language "run TC-001 and TC-002" message actually starts a real run (mocked Claude deciding to call the tool, real store interaction).
- Manual: complete a run, view the HTML report, download the PDF, confirm screenshots render correctly in both. Ask the chat "run TC-003 and let me know" and confirm it actually starts the run and the reply references the real `run_id`.

## Sizing

M (1–1.5 weeks).

## Status: Code complete, live verification pending an API key

All scope above is built:

- `agent/reporter.py` + `agent/templates/report.html.jinja2` — deterministic Jinja2 rendering (autoescaped) of a `TestRunDetail` + its `ExecutionStep`s into an HTML report: summary cards (status/passed/failed/total), a step-summary table, and a detailed execution-trace table with selector strategy, verifier confidence, and embedded screenshots where present.
- PDF export: **Playwright's own print-to-PDF**, not weasyprint — Chromium is already a hard dependency for the whole agent, and weasyprint's Cairo/Pango system libraries are a common source of install friction for one feature. `render_pdf()` loads the same rendered HTML into a throwaway headless page and calls `page.pdf()`.
- New `app/config.py::BACKEND_BASE_URL` so screenshot `<img>` tags resolve to absolute URLs — needed because Playwright's `page.set_content()` has no origin to resolve relative `/screenshots/...` paths against.
- New endpoints: `GET /test-runs/{run_id}/report` (HTML) and `GET /test-runs/{run_id}/report.pdf` (`backend/app/routers/reports.py`), both 404 for an unknown run.
- `ChatService` gained exactly one tool, `start_test_run(test_case_ids)`, via `client.beta.messages.tool_runner` (the sync tool runner, matching this file's existing sync `anthropic.Anthropic` client rather than switching to async). The tool validates IDs against `agent.runner.list_test_cases()` before calling `store.create_test_run()` and scheduling `run_test_suite` via the same `BackgroundTasks`/`event_bus` DI the REST endpoint uses — `chat.py`'s `get_chat_service` dependency now injects both. `reply_to()` switched from `client.messages.create()` to the tool runner, which transparently falls back to plain conversation when Claude doesn't call the tool.
- Frontend: `getReportUrl`/`getReportPdfUrl` in `api.js`; a "View Report · Download PDF" link pair on `TestRunnerPanel.jsx`, shown only once a run reaches a terminal status (passed/failed/error).
- 12 new tests: `test_reporter.py` (4 — all-pass, mixed-failure with screenshot/confidence, HTML-escaping of untrusted step content, empty-run rendering), `test_reports_router.py` (4 — including a **real** Playwright-rendered PDF asserted by its `%PDF` magic bytes, not mocked), `test_chat_tool_use.py` (3 — a scripted tool-call double that actually invokes the real `start_test_run` function, confirming real `store.create_test_run` + `background_tasks.add_task` calls and that an unknown test case ID is rejected without touching the store). Existing `test_chat.py`/`test_chat_service.py` updated for the `tool_runner` switch. Full suite: 82 passed, 1 skipped. Frontend: `vitest run` 3/3, `vite build` clean.

**What's not yet done:** the manual verification step — ask the chat "run TC-003 and let me know" against a live Claude call and confirm the reply references a real `run_id` — needs the still-withheld `ANTHROPIC_API_KEY`, same gap as phases 3–5. Report content itself (HTML/PDF rendering, screenshot embedding) has been verified for real, independent of that key, since it's pure templating over data the tests construct directly.
