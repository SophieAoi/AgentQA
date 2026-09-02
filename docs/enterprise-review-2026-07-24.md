# Engineering Audit Report — AgentQA (INFLUENCE QA)

**Project:** AgentQA / INFLUENCE QA — internal AI-driven QA agent tool
**Date:** 2026-07-24
**Scope:** Full-codebase enterprise audit — 6 skills run in parallel (codebase-audit, perf-engineer, clean-architecture, security-audit, senior-backend, senior-frontend). 13 source files analyzed (entire repo).
**Classification:** FULLSTACK — FastAPI 0.x (Python 3.11, unpinned) + React 18.2 / Vite 5. No database (in-memory store), no auth, no compliance requirements detected, SMALL scale tier, NONE observability maturity.

---

## Executive Summary

AgentQA is a small, early-stage internal QA tool (explicitly mid-way through a multi-phase build plan — chat and test-execution are deliberate stubs awaiting a real LLM/Playwright integration). The codebase is clean for its size: good module separation, a swap-friendly store API, and no critical vulnerabilities. Nothing here is on fire. Four issues are worth fixing before the next phase lands: a **confirmed** Pydantic timestamp bug (empirically reproduced against the installed version), an unsafe frontend polling lifecycle (leaks + infinite retry on error), a single architectural root cause (bare global module state) that independently shows up as a security, scalability, and testability problem across four of the six skill reports, and the complete absence of authentication — currently harmless on localhost, but a hard blocker the moment a second user or non-loopback network is involved. Zero test coverage exists anywhere in the repo, which should be addressed before any of the refactor-shaped fixes below are applied.

---

## Risk Dashboard

| ID | Severity | Category | Sources | Confidence | Effort | Priority |
|----|----------|----------|---------|------------|--------|----------|
| F-001 | HIGH | Correctness (confirmed bug) | codebase-audit, perf-engineer, clean-architecture, senior-backend | **High** — empirically reproduced | XS | Immediate |
| F-002 | HIGH | Frontend reliability (compound) | codebase-audit, perf-engineer, clean-architecture, senior-frontend | High | S | Immediate |
| F-003 | HIGH | Architecture (systemic root cause) | security-audit, clean-architecture, codebase-audit, senior-backend, perf-engineer | High | M | Sprint 1 |
| F-004 | HIGH | Security — no auth | security-audit | High | L | Quarter (blocker before multi-user) |
| F-005 | MEDIUM | Reliability (compound) | senior-backend, perf-engineer, senior-frontend | High | S | Immediate |
| F-006 | MEDIUM | Config hygiene | codebase-audit, clean-architecture, senior-frontend, security-audit | High | S | Sprint 1 |
| F-007 | MEDIUM | Supply chain | codebase-audit, security-audit, senior-backend | High | XS | Immediate |
| F-008 | MEDIUM | Security (self-XSS class) | security-audit, senior-frontend | Medium | XS | Sprint 1 |
| F-009 | MEDIUM | Architecture — layering | clean-architecture | High | S | Sprint 1 |
| F-010 | MEDIUM | Security — DoS/input limits | security-audit | Medium | S | Sprint 1 |
| F-011 | LOW | Accessibility | senior-frontend | Medium | S | Quarter |
| F-012 | LOW | Security headers | security-audit | High | XS | Quarter |
| F-013 | LOW | Security — /docs exposure | security-audit | Medium | XS | Quarter |
| F-014 | LOW | Type safety | senior-backend | Medium | XS | Quarter |
| F-015 | LOW | Duplication | codebase-audit | High | XS | Quarter |
| F-016 | LOW | Deprecation | codebase-audit | High | XS-S | Quarter |
| F-017 | LOW | React fragility | senior-frontend | Low | XS | Quarter |
| F-018 | LOW | Architecture — abstraction leak | clean-architecture | High | XS | Sprint 1 |
| F-019 | LOW | Error handling | senior-frontend | Medium | XS | Quarter |
| F-020 | LOW | Architecture — prop drilling | clean-architecture | Medium | S | Quarter |

**Findings summary:** 🔴 CRITICAL: 0 · 🟠 HIGH: 4 · 🟡 MEDIUM: 6 · 🟢 LOW: 10 (20 total, all shown above — no appendix needed at this count)

---

## Critical & High Findings

### F-001 — `ChatMessage.timestamp` default is frozen at import time (confirmed bug)
**Location:** `backend/app/models/schemas.py:20` — `timestamp: datetime = datetime.utcnow()`
**Flagged by:** codebase-audit, perf-engineer, clean-architecture, senior-backend
**Confidence: High** — senior-backend reproduced this directly against the installed Pydantic 2.13.4: two `ChatMessage()` instances created 1.5s apart returned an *identical* timestamp, because the default is computed once when the class body executes, not per-instance.
**Failure scenario:** Every current call site (`store.add_chat_message`) passes `timestamp` explicitly, so this is dormant today — but any future direct `ChatMessage(...)` construction (a test fixture, a new endpoint, a script) that omits `timestamp` silently gets a stale, server-start-time value with no error.
**Fix:** `timestamp: datetime = Field(default_factory=datetime.utcnow)`. XS effort, purely corrective, no behavior change to existing callers.

### F-002 — Test-run polling lifecycle is unsafe (compound)
**Location:** `frontend/src/components/TestRunnerPanel.jsx:33-40`
**Flagged by:** codebase-audit, perf-engineer, clean-architecture, senior-frontend
**Confidence: High**
Three sub-issues compound into one fix:
1. `setInterval` is only cleared on next-run-start or terminal status — never on component unmount (no `useEffect` cleanup).
2. The interval callback has no try/catch — a single failed `getTestRun` call throws an unhandled rejection but does **not** stop the interval, so on a backend hiccup it polls forever with no user-visible error state.
3. No backoff/timeout — a permanently unreachable backend polls at a fixed 1.5s cadence indefinitely.
**Failure scenario:** Today the component never unmounts mid-poll (no routing/conditional rendering exists yet), so this is currently dormant — but it activates the moment any tab-switching or routing is added (likely soon, given the project's stage), and the error-swallowing half is live today: if the backend restarts mid-run, the frontend silently spins forever with the last-known "running" state.
**Fix:** Move polling into a `useEffect` keyed on `run?.run_id`, with a cleanup `clearInterval` and a try/catch that sets a visible error/terminal state on repeated failure. S effort. **Write a characterization test for the current polling behavior before refactoring — zero tests exist today (see Testing Gap below).**

### F-003 — Global mutable module state is the root cause behind four independent findings
**Location:** `backend/app/services/store.py:20-21` (`chat_history`, `test_runs`)
**Flagged by:** security-audit (V1 — cross-user data leakage), clean-architecture (B4 — no DI seam), codebase-audit (F5/F6 — multi-worker breakage + unbounded growth), senior-backend (#3 — multi-worker footgun, #4 — `setattr` bypasses Pydantic validation), perf-engineer (P1 — unbounded memory)
**Confidence: High**
**This is the report's one systemic insight:** four different skills, working independently, all traced a distinct symptom back to the same design choice — bare module-level globals instead of a store hidden behind a `Depends()`-injected interface:
- **Security (V1):** every client shares the same `chat_history`/`test_runs` — the instant a second user touches this tool, they see each other's data. No auth (F-004) makes this worse, but the isolation gap exists independent of auth.
- **Scalability (codebase-audit F5, senior-backend #3):** `uvicorn --workers N > 1` gives each worker its own copy of the globals — a run started on worker A returns 404 when polled and routed to worker B.
- **Memory (perf-engineer P1):** `chat_history` and `test_runs` never get evicted; unbounded growth for the life of the process.
- **Testability (clean-architecture B4):** no DI seam means tests can't isolate state without monkeypatching module globals.
**Fix (per clean-architecture's roadmap):** introduce a `StoreProtocol` behind `Depends(get_store)`. This single seam is what actually makes the already-planned MongoDB swap (the store module's own docstring anticipates this) *and* future auth-scoping *and* multi-worker safety cheap — doing it now, before Phase 5/6 real-agent logic lands on top, is meaningfully cheaper than doing it after. M effort. See ADR-001 below.

### F-004 — No authentication or authorization on any endpoint
**Location:** `backend/app/main.py`, both routers
**Flagged by:** security-audit (V2)
**Confidence: High**
**Failure scenario:** Not exploitable today — the tool runs on localhost with CORS restricted to `localhost:5173`/`localhost:3000`. It becomes immediately exploitable (any network peer can read all chat/test data and trigger unbounded test runs) the moment this binds to a LAN/VPN interface or a shared host, which is a realistic near-term step for an internal QA tool used by more than one person.
**Fix:** Add an identity layer (even a simple shared API key or SSO-backed session) before any non-loopback deployment. This depends on F-003's DI seam existing first — auth middleware needs somewhere to inject the authenticated identity that the store can then scope by. L effort; not urgent for continued solo-localhost use, but should gate any deployment decision.

---

## Medium & Low Findings (grouped)

**F-005 — Silent failure chain (background task crash → stuck "running" forever).** `agent_runner.run_test_suite()` has no try/except; a mid-run exception is only logged server-side, the client polls a run permanently stuck at `"running"` with no failure signal (senior-backend #2, corroborated by perf-engineer's poll-forever finding on the frontend side). Confidence: High. Fix: wrap the loop body, set `status=error` on exception. S effort.

**F-006 — Hardcoded config across the stack.** `frontend/src/api.js:1` (`API_BASE`), `backend/app/main.py:21-27` (CORS origins, acknowledged TODO), `SiteViewer.jsx:4` (staging URL) — none read from environment. Breaks the moment any build target isn't localhost. Confidence: High. S effort — move to `import.meta.env.VITE_API_BASE` / `os.environ`.

**F-007 — Unpinned dependencies.** `requirements.txt` lists `fastapi`, `uvicorn[standard]`, `pydantic` with zero version pins and no lockfile — non-reproducible builds, silent breaking upgrades, supply-chain exposure. Confidence: High. XS effort — pin + add a lockfile.

**F-008 — `javascript:` URI in SiteViewer address bar.** `SiteViewer.jsx:31` renders unsanitized user input into `<a href>`. Self-XSS class only (requires the user to paste the payload into their own bar); `rel="noreferrer"` already mitigates tabnabbing. Confidence: Medium. XS effort — allowlist `http:`/`https:` schemes.

**F-009 — Business logic embedded in router.** `reply_to()` in `chat.py:19-44` is agent-reasoning logic living in the presentation layer; belongs in a `chat_service`. Confidence: High. S effort.

**F-010 — No rate limiting / unbounded input lengths.** `message` and `test_case_ids` have no length caps; no request throttling. Confidence: Medium. S effort.

**F-011 — Accessibility gaps.** No `<label>`/`aria-label` on the URL input or chat textarea; no `aria-live` region for streaming chat/run updates. Confidence: Medium. S effort.

**F-012 — Missing security headers** (CSP/HSTS/X-Frame-Options/X-Content-Type-Options). Confidence: High. XS effort, low current impact (localhost API).

**F-013 — `/docs` OpenAPI UI auto-exposed.** Fine for dev, gate before any prod deploy. Confidence: Medium. XS effort.

**F-014 — `TestStepResult.status` is a free-form string, not an `Enum`** — nothing prevents a typo'd status value. Confidence: Medium. XS effort.

**F-015 — Duplicated fetch error-handling boilerplate** across all 4 functions in `api.js` — extract a shared `request()` helper. Confidence: High. XS effort.

**F-016 — `datetime.utcnow()` used throughout** — deprecated in Python 3.12+, naive datetimes risk future tz-aware comparison bugs. Confidence: High. XS-S effort to batch-fix.

**F-017 — Index-based React `key` props** on `chat_history`/`steps`/`logs` lists — harmless while append-only, fragile the moment removal/reorder is added. Confidence: Low. XS effort.

**F-018 — `store.py`'s otherwise-clean write API leaks its read paths.** `chat.py:49` and `test_runs.py:39` reach directly into `store.chat_history`/`store.test_runs` globals instead of going through an accessor — undermines the store's own "swap-friendly" design goal for exactly those two paths. Confidence: High. XS effort — add `get_chat_history()`/`list_test_runs()` accessors.

**F-019 — Initial chat history load silently swallows errors** (`ChatSidebar.jsx:11-13`, empty catch). Confidence: Medium. XS effort.

**F-020 — `run`/`setRun` prop-drilled through `ChatSidebar`** into `TestRunnerPanel` — a small `RunContext` removes it. Low urgency; the lift itself is a reasonable, deliberate choice for the current tree depth. Confidence: Medium. S effort.

---

## Systemic Issue (Cross-Skill Insight)

**One root cause, four symptoms.** F-003 is this audit's single most valuable finding *because* no individual skill would have surfaced it as clearly alone — security saw a data-leakage bug, perf saw a memory leak, architecture saw a missing DI seam, and backend saw a multi-worker footgun. They're the same fifteen lines of code (`store.py`'s two module-level containers). Fixing it once (StoreProtocol + `Depends`) resolves all four instead of four separate patches — this is the highest-leverage single change in the report, and the natural companion to the already-planned MongoDB swap.

**Secondary pattern:** three findings (F-002, F-005, plus perf-engineer's P7) all stem from **the same missing habit — no error path is ever surfaced from a long-running/background operation to its caller**, on both the background-task side (F-005) and the polling side (F-002). Worth fixing as one reliability pass rather than two unrelated tickets.

---

## Testing Gap (cuts across all 6 skill reports)

Every skill that touched code quality independently flagged the same thing: **zero tests exist anywhere in this repo** (no pytest, no vitest/jest, no test files). Both senior-backend and senior-frontend explicitly returned `Overall: NEEDS_TESTING` rather than a quality verdict, because their own workflow rules require a behavior-preservation baseline before recommending refactors like F-002 or F-003's DI seam. **Before applying any of the Immediate/Sprint-1 fixes below that touch behavior (F-002, F-003, F-005, F-009), write a minimal characterization test first** — this is cheap now (small codebase) and expensive to retrofit later.

---

## Compliance Status
Not applicable — no compliance requirements (GDPR/SOC2/HIPAA/PCI) detected in this codebase.

## Observability Assessment
**Current maturity: NONE** — no structured logging, no request IDs, no metrics, no tracing. Acceptable for the current single-developer, pre-Phase-5/6 stage. The highest-leverage add, when Phase 5/6 lands, is structured per-run logging on the background test-runner specifically (`agent_runner.py`) — it's exactly the kind of long-lived async work that's otherwise painful to debug blind, and F-005's silent-failure chain is a direct symptom of having no visibility into that path today.

---

## Recommended Action Plan

### Immediate (this week)
- F-001 — Fix `ChatMessage.timestamp` default (`Field(default_factory=...)`). XS.
- F-002 — Fix polling lifecycle (cleanup + try/catch). S. *Write a characterization test first.*
- F-005 — Add error handling to `run_test_suite`, surface `status=error`. S.
- F-007 — Pin backend dependencies. XS.

### Sprint 1 (next 2 weeks)
- F-003 — Introduce `StoreProtocol` + `Depends(get_store)` DI seam (see ADR-001). M.
- F-018 — Close the two store read-path leaks (quick win, independent of full F-003 refactor). XS.
- F-006 — Move hardcoded config (API base, CORS, staging URL) to env vars. S.
- F-008 — Allowlist `http:`/`https:` schemes in SiteViewer navigation. XS.
- F-009 — Extract `reply_to()` into a `ChatService`. S.
- F-010 — Add input length limits on chat/test-run requests. S.

### Quarter (next 90 days) / before any multi-user or non-loopback deployment
- F-004 — Add real authentication/authorization (hard blocker before deploy). L.
- F-011, F-012, F-013, F-014, F-015, F-016, F-017, F-019, F-020 — batch as a hygiene/polish pass.
- Stand up a minimal test suite (pytest + vitest) — currently the single biggest structural gap blocking safe iteration on everything above.

---

## Architecture Decision Record

### ADR-001: Introduce a `StoreProtocol` DI seam ahead of the MongoDB swap
**Date:** 2026-07-24 · **Status:** Proposed · **Deciders:** Engineering team

**Context:** `store.py` already anticipates a future swap from in-memory dicts to MongoDB — its own docstring says the function signatures are "designed to make that swap easy later." But four independent audit findings (F-003) show the *current* implementation doesn't fully achieve that: module-level globals leak cross-request state (security), break under multiple uvicorn workers (scalability), grow unbounded (memory), and can't be isolated in tests (testability).

**Decision:** Define a `StoreProtocol` (chat + test-run methods) and inject the concrete implementation via FastAPI's `Depends(get_store)` instead of importing the `store` module directly in routers/services.

**Rationale:** This is the one change that unlocks the MongoDB swap *and* solves the security/scalability/testability symptoms simultaneously, rather than patching each symptom separately. It's also strictly additive — no behavior change for current callers, just an indirection layer.

**Consequences:**
- *Positive:* MongoDB swap becomes a single new class, not a router-touching change; auth-scoping (F-004) has somewhere to plug in; tests can inject a fake store; multi-worker deploys become possible.
- *Negative:* Small amount of upfront boilerplate (protocol + factory); every router/service call site needs updating from `store.X()` to an injected `store: StoreProtocol` parameter — a mechanical but real touch across ~6 files.

**Alternatives considered:**
1. *Do nothing until the Mongo swap itself* — rejected: the swap would then have to fix the DI gap and change persistence at the same time, compounding risk in one PR instead of two small ones.
2. *Add a global lock / thread-safety wrapper instead of DI* — rejected: solves the memory/concurrency symptom but not the security (cross-user leakage) or testability symptoms; DI is the only option that addresses all four.

---

## PR Description Templates

### PR-1: Fix ChatMessage timestamp default (F-001)
**Problem:** `ChatMessage.timestamp` default is computed once at class-definition time, not per-instance — dormant today, but a landmine for any future call site that omits `timestamp`.
**Solution:** Switch to `Field(default_factory=datetime.utcnow)`.
**Changes:** `backend/app/models/schemas.py:20`
**Testing:** Add a test asserting two `ChatMessage()` instances created moments apart have different timestamps.
**Breaking changes:** None.

### PR-2: Harden test-run polling lifecycle (F-002)
**Problem:** Polling interval has no unmount cleanup and no error handling — leaks on unmount, spins forever on a backend hiccup with no user feedback.
**Solution:** Move polling into a `useEffect` keyed on `run?.run_id`, add cleanup `clearInterval`, wrap the fetch in try/catch with a visible error state.
**Changes:** `frontend/src/components/TestRunnerPanel.jsx:33-40`
**Testing:** Characterization test for current polling behavior first (none exists today), then a test for the unmount-cleanup and error-path behavior.
**Breaking changes:** None (internal implementation only).

### PR-3: Add StoreProtocol DI seam (F-003 / ADR-001)
**Problem:** Global module state causes cross-user data leakage, multi-worker breakage, unbounded growth, and untestable routers — four separate findings, one root cause.
**Solution:** Define `StoreProtocol`, inject via `Depends(get_store)`, update all router/service call sites.
**Changes:** `backend/app/services/store.py`, `backend/app/routers/chat.py`, `backend/app/routers/test_runs.py`, `backend/app/services/agent_runner.py`
**Testing:** Add a fake in-memory `StoreProtocol` implementation for tests; verify all existing endpoints behave identically before/after.
**Breaking changes:** None externally (API contracts unchanged); internal call sites all change from module import to injected parameter.

### PR-4: Handle background test-run failures (F-005)
**Problem:** An exception inside `run_test_suite` leaves the run permanently stuck at `"running"` with no client-visible failure.
**Solution:** Wrap the loop body in try/except; on exception, call `store.update_test_run(run_id, status=TestRunStatus.error, finished_at=...)`.
**Changes:** `backend/app/services/agent_runner.py:52-109`
**Testing:** Add a test that forces an exception mid-run and asserts the run reaches `status=error` rather than staying `running`.
**Breaking changes:** None.

---

## Overall

**OVERALL: NEEDS_WORK**

Basis (worst-of the six sub-skill verdicts, mapped to the SHIP_READY / NEEDS_WORK / BLOCKED scale): codebase-audit=TECH_DEBT, perf-engineer=NEEDS_OPTIMIZATION, clean-architecture=NEEDS_REFACTOR, security-audit=AT_RISK, senior-backend=NEEDS_TESTING, senior-frontend=NEEDS_TESTING — all map to NEEDS_WORK. No skill returned a CRITICAL/BLOCKED-tier verdict (0 CRITICAL findings across all 20), and none returned SHIP_READY (zero test coverage and the F-003 systemic gap rule that out). Nothing here blocks continued local development; F-004 (auth) is the one item that should block any move beyond solo-localhost use.

**Top 3 immediate actions:**
1. F-001 — Fix `ChatMessage.timestamp` default (XS effort, confirmed bug)
2. F-002 — Harden test-run polling lifecycle (S effort, compound reliability fix)
3. F-007 — Pin backend dependencies (XS effort, closes supply-chain gap)

**Artifacts generated:**
- 📄 This formal audit report (`docs/enterprise-review-2026-07-24.md`)
- 📄 Sub-reports: `docs/architecture-review-2026-07-24.md`, `docs/security-audit-fullstack-2026-07-24.md`
- 📐 1 ADR (ADR-001: StoreProtocol DI seam)
- 🔀 4 PR description templates (F-001, F-002, F-003, F-005)

No code was changed as part of this audit — it is analysis-only. No commit was made; nothing here needs a commit message until a fix is applied.
