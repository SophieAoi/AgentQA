# Phase 5 — Live streaming (WebSockets)

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context and design rationale — especially "Streaming comes after the agent loop works headlessly."

## Goal

Now that the agent loop is proven correct headlessly (phases 3–4), add real-time visibility — live logs and live browser screenshots — without touching the underlying execution logic.

## Scope — build

- `WS /ws/test-runs/{run_id}/logs` — pushes `AgentTrace`/`ExecutionStep` events as they're written to the store. Simplest viable implementation: an in-process pub/sub (an `asyncio.Queue` per active `run_id`) that `agent_runner`/`agent/executor.py` publish to, and the WS endpoint subscribes to. Stays workable at single-process scale; documented upgrade path to Redis pub/sub if/when multi-worker lands (backlog) — not built now.
- `WS /ws/test-runs/{run_id}/browser` — periodic (every 1–2s during active execution, or after every tool call) screenshot-frame push (base64 or a freshly-written file URL). **Not** a live remote-DOM/iframe embed — `SiteViewer.jsx`'s existing comment already explains why that's unworkable here (OAuth + third-party-cookie blocking). This replaces phase 2's screenshot-on-error-only pattern with something closer to real-time, while keeping the same "not a true iframe" constraint.
- Frontend: `SiteViewer.jsx`'s log panel switches from `run?.logs` (polled) to a WS subscription when a run is active, falling back to the existing poll-based flow for run start/final-status (POST to start + WS to watch; `GET /test-runs/{id}` stays the source of truth for final state and page-load/reconnect scenarios).
- Reconnect handling on the frontend: WS drops are expected — reconnect with backoff, and on reconnect fall back to a `GET /test-runs/{id}` call to reconcile any missed events (stream-first, then reconcile with history).

## Explicitly NOT in scope

- True live embedded browser view/remote control.
- Video recording (backlog).
- Any change to Planner/Executor/Verifier logic — this phase is purely a new transport layer over data that already exists.

## Files to create/modify

- New: `backend/app/routers/websockets.py` — `WS /ws/test-runs/{run_id}/logs`, `WS /ws/test-runs/{run_id}/browser`.
- New: `backend/app/services/event_bus.py` — pub/sub mechanism, injected alongside `StoreProtocol`.
- `agent/executor.py`, `agent/runner.py` — publish events as they occur (in addition to writing to the store, not instead of).
- `frontend/src/components/SiteViewer.jsx` — WS subscription + reconnect logic, screenshot-frame rendering.
- New: `frontend/src/hooks/useRunStream.js` — encapsulates WS lifecycle, mirroring phase 0's `useTestRun` polling hook's cleanup-on-unmount discipline.
- `backend/tests/test_websockets.py` — using FastAPI's `TestClient` WS support, assert events published during a run reach a connected client in order.

## Verification

- `pytest` WS test: start a run, connect a test WS client, assert log/step events arrive as the (mocked) agent runner publishes them.
- Manual: watch a real run live in the browser — logs streaming, screenshots updating. Kill the backend mid-run, restart, confirm the frontend recovers via the `GET` fallback rather than hanging.
- Regression: confirm the phase 0–4 polling-only flow still works if a client never connects to the socket — WS is additive, never a hard dependency for run correctness.

## Sizing

M (1–1.5 weeks).

## Status: Code complete, live verification pending an API key

All scope above is built:

- `app/services/event_bus.py` — `EventBus`, in-process pub/sub keyed by `(run_id, channel)` so a "logs"-only subscriber never receives large "browser" screenshot payloads. `publish()` is a cheap no-op when no client is connected — never blocks the agent loop.
- `WS /ws/test-runs/{run_id}/logs` and `WS /ws/test-runs/{run_id}/browser` in `app/routers/websockets.py`. Both 404 (WS close code 4004) for an unknown `run_id`, send one `{"type": "connected", ...}` hello on open, then relay published events as `{"type": ..., "data": ...}`.
- `event_bus` threaded alongside `store` through the full call chain that already carried `store`: `app/routers/test_runs.py` → `app/services/agent_runner.py` → `agent/runner.py` → `agent/executor.py` → `agent/tools/playwright_tools.py` (`ToolContext`). Every existing `store.add_log`/`on_step` call site now also publishes.
- Screenshot frames: `_record_step()` in `playwright_tools.py` — the single choke point every tool call (click/fill/select/navigate/read_page/assert_condition) already passes through — now also captures a cheap JPEG (`quality=50`) after every tool call and publishes it as a base64 data URL on the `"browser"` channel. Best-effort: a failed screenshot capture is swallowed, never breaks the tool call itself.
- Frontend: new `frontend/src/hooks/useRunStream.js` (mirrors `useTestRun.js`'s cleanup-on-unmount discipline) owns both WS connections for an active run, with reconnect-with-backoff (1s → 2s → 4s → capped at 10s) per socket. `SiteViewer.jsx` prefers the WS-streamed log lines while connected and the run is active, falling back to the polled (authoritative) `run.logs` otherwise — and renders the latest screenshot frame when one has arrived. A small "● Live / ○ Connecting…" indicator surfaces stream state.
- Reconciliation: rather than building a separate reconnect-then-GET reconciliation path, this reuses `useTestRun.js`'s existing 1.5s poll of `GET /test-runs/{id}`, which keeps running unconditionally alongside the WS stream — so `run.logs`/`run.steps` are always eventually consistent regardless of any WS gap, drop, or a client that never connects to the socket at all. WS is purely additive, exactly as scoped.
- 5 new backend tests (`test_websockets.py`, via FastAPI's `TestClient` WS support): unknown-run close code, hello message, in-order relay, channel isolation (a "logs" publish never reaches a "browser" subscriber), and unsubscribe-on-disconnect. Existing Phase 2–4 tests updated for the new `event_bus` parameter threaded through `run_test_suite`/`run_test_case`/`run_executor`. Full backend suite: 71 passed, 1 skipped (the Phase 3 live-API integration test, still gated on `ANTHROPIC_API_KEY`). Frontend: existing `useTestRun.test.js` still passes; `vite build` and `vitest run` both clean.

**What's not yet done:** the manual verification step (watch a real run live in the browser, kill the backend mid-run and confirm recovery) needs a real run, which needs the still-withheld `ANTHROPIC_API_KEY` — same gap as phases 3–4. The regression check (polling-only flow still works with zero WS clients) is true by construction here, since `store`/`run.logs`/`run.steps` writes are unchanged and WS only adds publish calls alongside them, but it's worth confirming visually once a real run is possible.
