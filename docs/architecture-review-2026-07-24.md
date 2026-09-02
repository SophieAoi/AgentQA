# Clean Architecture Review — AgentQA (INFLUENCE QA)

Date: 2026-07-24
Scope: single-dimension architecture (concern separation, coupling, layering, DI, migration roadmap).
Stage context: Phase 5–6 early build. Stubs in `chat.py` / `agent_runner.py` and the in-memory `store.py` are deliberate, ADR-lite decisions and are NOT flagged as defects. This review evaluates whether those seams actually hold.

Stack: FastAPI (Python 3.11), Pydantic v2 schemas, in-memory dict store, React 18.2 + Vite, fetch-based `api.js`.

---

## Verdict on the deliberate in-memory store decision

The store's *write* API is a clean seam: `add_chat_message`, `create_test_run`, `get_test_run`, `update_test_run`, `add_log` are all function-based and swap-friendly, exactly as the docstring intends. **But two reads bypass the seam and reach into module globals directly**, which breaks the "swap to MongoDB easily" goal for those paths. That is the core structural finding below.

---

## Findings (confidence-scored)

### Backend

**B1 — Store leaks internal data structures to routers (repository-abstraction leak).** `chat.py:49` returns `store.chat_history` (the raw global list) and `test_runs.py:39` does `list(store.test_runs.values())` (reaches into the raw global dict). There is no `get_chat_history()` / `list_test_runs()` accessor, so a MongoDB swap would force edits in the routers, not just in `store.py` — the one thing the design set out to avoid. **Confidence: High** — direct global access is visible at those two lines.

**B2 — Business/application logic lives in the presentation layer.** `reply_to()` (`chat.py:19–44`) is agent reasoning logic embedded in a router. When the real Claude call lands it will grow; it belongs in an application service (`services/chat_service.py`). **Confidence: High** — logic is literally defined inside the router module.

**B3 — No application/service layer between routers and store.** Routers call `store.*` directly (`chat.py:54–56`, `test_runs.py:24,39`). Acceptable at this size, but every future cross-cutting concern (auth, validation, orchestration) has nowhere to live except the router. Frame as next-step, not urgent. **Confidence: High** — verified by import graph; routers import `store` directly.

**B4 — Global mutable singleton coupling / no DI.** `agent_runner.py` and both routers import the `store` module and mutate its module-level globals. Nothing is injected via FastAPI `Depends()`, so tests must monkeypatch globals and state bleeds across tests/requests. **Confidence: High** — no `Depends` on any store access anywhere.

**B5 — Shared-instance timestamp bug.** `schemas.py:20` `timestamp: datetime = datetime.utcnow()` is evaluated once at class-definition (import) time; every `ChatMessage` created without an explicit timestamp shares the process-start time. Use `Field(default_factory=datetime.utcnow)`. This is an actual correctness bug, not just style. **Confidence: High** — standard Python default-evaluation semantics.

**B6 — Minor: `_now()` helper with in-function import.** `agent_runner.py:112–114` re-imports `datetime` inside a helper while `create_test_run` already uses `datetime.utcnow()` inline elsewhere — inconsistent time access. Centralize a clock. **Confidence: Low** — cosmetic, no behavior impact.

### Frontend

**F1 — Polling interval leaks on unmount.** `TestRunnerPanel.jsx:34` starts `setInterval` inside `handleRunTests` but there is no `useEffect` cleanup clearing `pollRef.current` on unmount; if the panel unmounts mid-run the timer keeps firing `setRun` on an unmounted tree. **Confidence: High** — no cleanup effect present.

**F2 — Data-fetching + polling mixed into a view component.** `TestRunnerPanel` owns selection UI, the network call, and the poll loop. Extracting a `useTestRun(runId)` hook isolates the async/lifecycle concern and fixes F1 for free. **Confidence: Medium** — clean-up rec, not a defect.

**F3 — Prop drilling `run`/`setRun` through `ChatSidebar`.** `ChatSidebar` (`:5,:83`) accepts and forwards `run`/`setRun` without using them — pure pass-through to `TestRunnerPanel`. Since `SiteViewer` also needs `run`, a small React context (`RunContext`) removes the drill. **Confidence: Medium** — noted as deliberate lift; low urgency at this scale.

**F4 — Hardcoded API base / config not isolated.** `api.js:1` hardcodes `http://localhost:8000`; same class as the hardcoded CORS origins (`main.py:23`) and staging URL (`SiteViewer.jsx:4`). Move to `import.meta.env.VITE_API_BASE`. **Confidence: High** — literal in source.

**F5 — No shared message contract.** Frontend builds `{role, content}` locally (`ChatSidebar.jsx:27,31`) while backend returns `ChatMessage` with a timestamp. Divergence risk once history rendering uses timestamps. **Confidence: Low** — cosmetic today.

---

## Target layering (FastAPI-idiomatic)

```
presentation   app/routers/*          thin HTTP; validate, delegate, serialize
application    app/services/*_service DEAL logic + orchestration (reply_to, run orchestration)
domain         app/models/schemas     entities / DTOs (already clean)
infrastructure app/repositories/*     store today, Mongo later — behind a Protocol
```

Introduce a `StoreProtocol` (Python `typing.Protocol`) that the in-memory store satisfies now and a `MongoStore` satisfies later; inject it via `Depends(get_store)`. This is the single change that makes the deliberate swap actually cheap.

---

## Migration roadmap (strangler, each step ships green)

1. **Close the store seam (B1).** Add `get_chat_history()` and `list_test_runs()` to `store.py`; switch `chat.py:49` and `test_runs.py:39` to call them. No behavior change. **Confidence: High / S.**
2. **Fix the timestamp bug (B5)** with `default_factory`. **High / S.**
3. **Extract `ChatService.reply_to` (B2)** into `app/services/chat_service.py`; router calls the service. **High / S.**
4. **Define `StoreProtocol` + `get_store` dependency (B4);** inject into routers/services; delete direct module globals from call sites. Unlocks the Mongo swap and real unit tests. **Medium / M** (touches all store callers).
5. **Frontend `useTestRun` hook (F1/F2)** with `clearInterval` cleanup; move config to env (F4). **High / M.**
6. **`RunContext` to drop prop drilling (F3).** **Medium / S.** Optional.

Steps 1–3 and 5's cleanup are safe now; step 4 is the strategic one to sequence before the MongoDB and real-agent phases land, so those phases build on the seam rather than around it.

---

## Score

- **Layering:** NEEDS_REFACTOR — logic in router, no application layer (B2/B3).
- **Coupling:** NEEDS_REFACTOR — global-singleton store access + two abstraction leaks (B1/B4).
- **Modularity:** WELL_ARCHITECTED — clean file/module split, sensible router grouping.
- **Testability:** NEEDS_REFACTOR — no DI, globals force monkeypatching.

**Overall: NEEDS_REFACTOR** — sound module boundaries and a genuinely swap-friendly *write* API, undercut by two store-abstraction leaks, business logic in the presentation layer, and no DI seam. All fixable with the small, non-behavioral steps above; appropriate to do before the Mongo and real-agent phases, not urgent for the current stub build.
