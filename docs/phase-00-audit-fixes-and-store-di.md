# Phase 0 — Audit fixes + StoreProtocol DI seam

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context and design rationale.

## Goal

Apply the audit-approved quick fixes and land the `StoreProtocol` DI seam (ADR-001) **before** any agent-execution state is added to the store — the agent engine (phases 2–4) is about to add substantially more state (steps, screenshots, reasoning traces) on top of `store.py`, and this refactor is cheaper now than after.

## Scope — build

- **F-001**: `ChatMessage.timestamp: datetime = Field(default_factory=datetime.utcnow)` in `schemas.py` — the current bare `datetime.utcnow()` default is evaluated once at class-definition time, not per-instance.
- **F-002**: Move `TestRunnerPanel`'s polling into a `useTestRun(runId)` hook using `useEffect` keyed on `run?.run_id`, with `clearInterval` cleanup on unmount/run-change, and a `try/catch` around `getTestRun` that surfaces a visible error state after repeated consecutive failures instead of silently polling forever.
- **F-007**: Pin `backend/requirements.txt` (`fastapi==`, `uvicorn[standard]==`, `pydantic==`) and add a lockfile.
- **F-005**: Wrap `run_test_suite`'s body in try/except; on any unhandled exception, set the run's `status=TestRunStatus.error` (and `finished_at`) instead of leaving it stuck at `running`.
- **F-009**: Extract `reply_to()` out of `chat.py` into `app/services/chat_service.py` as `ChatService.reply_to()` — gives phase 1's real Claude wiring a natural home.
- **F-018**: Add `get_chat_history()` / `list_test_runs()` accessors to the store; stop routers reaching directly into `store.chat_history` / `store.test_runs`.
- **ADR-001**: Define `StoreProtocol` (a `typing.Protocol`) covering all store operations (`add_chat_message`, `get_chat_history`, `create_test_run`, `get_test_run`, `list_test_runs`, `update_test_run`, `add_log`). Move the current dict-backed logic into a concrete `InMemoryStore(StoreProtocol)` class (still in-memory — only the seam changes, not the storage). Add a `get_store()` FastAPI dependency (module-level singleton instance) and switch `chat.py`, `test_runs.py`, and `agent_runner.py` to accept `store: StoreProtocol = Depends(get_store)` instead of importing the module directly.
- Stand up the **first test infrastructure in the repo** — currently zero tests exist anywhere.

## Explicitly NOT in scope

- Postgres/SQLAlchemy (the store stays in-memory; only the seam changes).
- Auth, multi-worker support.
- CORS/env config cleanup (F-006) and rate limiting (F-010) — defer to phase 1's config work or a later hygiene pass.
- Any new agent/execution functionality — this phase only touches existing chat + test-run plumbing.

## Files to create/modify

- `backend/app/services/store.py` — add `StoreProtocol`, rename current logic into `InMemoryStore`, add `get_store()`.
- `backend/app/services/chat_service.py` — new; `ChatService` class wrapping `reply_to()`.
- `backend/app/routers/chat.py` — switch to `Depends(get_store)` / `Depends(get_chat_service)`, use `get_chat_history()` accessor.
- `backend/app/routers/test_runs.py` — switch to `Depends(get_store)`, use `list_test_runs()` accessor.
- `backend/app/services/agent_runner.py` — accept injected store, wrap `run_test_suite` in try/except.
- `backend/app/models/schemas.py` — F-001 fix.
- `backend/requirements.txt` + new lockfile.
- `frontend/src/hooks/useTestRun.js` — new; extracted polling hook (F-002 fix).
- `frontend/src/components/TestRunnerPanel.jsx` — use the new hook instead of inline `setInterval`.
- New: `backend/tests/conftest.py`, `backend/tests/test_store.py`, `backend/tests/test_chat.py`, `backend/tests/test_test_runs.py`.
- New: `frontend/vitest.config.js` (or equivalent) + `frontend/src/hooks/useTestRun.test.js`.

## Verification

- `pytest` green:
  - Existing endpoint behavior unchanged (same status codes/response shapes) after switching to the injected store.
  - New test: two `ChatMessage()` instances created moments apart have different timestamps.
  - New test: forcing an exception inside `run_test_suite` results in the run reaching `status=error`, not staying `running`.
- Frontend:
  - New test asserts `clearInterval` fires on unmount and on `run_id` change.
  - New test asserts a failed poll surfaces a visible error state after N retries instead of silently retrying forever.
- Manual: `uvicorn app.main:app --reload` still boots; `GET /docs` still lists identical routes; existing frontend flow (start a run, watch it poll to completion) behaves identically to pre-phase-0.
- Dependency pins installed cleanly in a fresh virtualenv.

## Sizing

S–M (2–4 days). Mechanical but touches ~7 backend files and 1–2 frontend files, and stands up test infrastructure from zero.
