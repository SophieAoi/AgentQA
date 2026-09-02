# Phase 7 — Real authentication

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context. This is the first item pulled out of the [phase 7+ backlog](phase-07-plus-backlog.md) and given its own phase doc, following that doc's own instruction to do so once an item is actually prioritized.

## Goal

Close audit finding F-004 (no authentication on any endpoint) with real per-user login, so the tool is no longer wide open to any network peer the moment it's reachable off localhost. Scoped per explicit user decision:

- **Mechanism: per-user login (username/password), not a shared API key or SSO.** Real accounts, hashed passwords, server-side sessions.
- **Data isolation: explicitly out of scope for this phase.** Every logged-in user still sees the same shared `chat_history`/`test_runs` data, exactly as today — auth only gates who can get in, it doesn't scope what they see once they're in. Per-user data isolation is a separate, larger future change (would require tagging every `ChatMessage`/`TestRunDetail` with an owning identity and filtering every read).

## Scope — build

- `User` model + `UserStore` (in-memory, same pattern as `InMemoryStore` — Postgres migration is a separate backlog item that will absorb this too) — `id`, `username`, `password_hash`, `created_at`.
- Password hashing via `bcrypt` (`bcrypt.hashpw`/`bcrypt.checkpw`) — a well-vetted, purpose-built library rather than hand-rolling KDF logic.
- Server-side opaque sessions (not JWT): a random session token mapped to a user id + expiry, held in-memory (`SessionStore`, same pattern again). Opaque tokens are trivially revocable (delete from the store) without signing-key management — the right tradeoff for a first cut with no distributed/multi-worker deployment yet.
- Session delivered via an `HttpOnly`, `SameSite=Lax` cookie — not `localStorage` (XSS-exfiltration resistant) and not a bearer header (avoids every frontend fetch call needing to manually attach it). `Secure` flag gated behind a config flag, off by default for local `http://localhost` dev, documented to turn on for any real deployment behind TLS.
- Endpoints: `POST /auth/login` (username+password → sets session cookie), `POST /auth/logout` (invalidates + clears), `GET /auth/me` (current user or 401 — what the frontend uses to decide whether to show the login screen).
- A `get_current_user` FastAPI dependency, required by every existing router: `chat`, `test_runs`, `test_cases`, `reports`, `websockets`. WebSocket auth reads the same session cookie off the upgrade request (browsers attach cookies to same-site WS handshakes automatically, so no separate WS auth handshake is needed).
- Bootstrapping the first account: an admin user auto-created on startup from `INITIAL_ADMIN_USERNAME`/`INITIAL_ADMIN_PASSWORD` env vars if the user store is empty — same pattern as `INFLUENCE_TEST_USERNAME`/`PASSWORD` in `app/config.py`. Once logged in, that user (or any user) can create more accounts via `POST /auth/register` — deliberately requires an existing valid session, so there's no open public signup.
- Frontend: `useAuth.js` hook (checks `GET /auth/me` on load), a `LoginScreen.jsx` gating the rest of the app when unauthenticated, a logout affordance in the chat sidebar header.
- Generic login error messages (don't reveal whether a username exists) as a baseline minimum; no plaintext password ever logged or persisted anywhere (mirrors the credential-redaction discipline already established in `agent/tools/redaction.py`).

## Explicitly NOT in scope

- Per-user data isolation/scoping of chat history or test runs (explicit user decision — later phase if ever needed).
- Password reset / email verification flows (no email sending capability exists anywhere in this project yet).
- Rate limiting on login attempts (audit finding F-010 territory — bundle with that fix, not this one).
- Role-based permissions (admin vs. regular user) — every authenticated user has identical access for now.
- Postgres-backed persistence for users/sessions (rides along whenever the phase 7+ backlog's Postgres migration item happens — `UserStore`/`SessionStore` are designed the same way `InMemoryStore` was, so that swap is a new class, not a redesign).

## Files to create/modify

- New: `backend/app/models/schemas.py` — add `User` (response-safe, no password hash), `LoginRequest`, `RegisterRequest`.
- New: `backend/app/services/auth_service.py` — password hashing/verification, session creation/validation, `UserStore`/`SessionStore` (extends the existing `store.py` pattern or lives alongside it).
- New: `backend/app/routers/auth.py` — `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`, `POST /auth/register`.
- `backend/app/main.py` — bootstrap the seed admin user on startup; register the auth router.
- `backend/app/config.py` — `INITIAL_ADMIN_USERNAME`, `INITIAL_ADMIN_PASSWORD`, `SESSION_COOKIE_SECURE`, `SESSION_TTL_SECONDS`.
- `backend/app/routers/chat.py`, `test_runs.py`, `test_cases.py`, `reports.py`, `websockets.py` — add the `get_current_user` dependency to every route.
- `backend/requirements.txt` — add `bcrypt`.
- New: `frontend/src/hooks/useAuth.js`, `frontend/src/components/LoginScreen.jsx`.
- `frontend/src/App.jsx` — gate the app behind `useAuth`.
- `frontend/src/api.js` — `login()`, `logout()`, `getCurrentUser()`; every existing `fetch()` call needs `credentials: "include"` so the session cookie is actually sent cross-port in local dev.
- New: `backend/tests/test_auth.py` — login success/failure, session cookie issued and validated, logout invalidates it, protected endpoints 401 without a session and succeed with one, register requires an existing session.

## Verification

- `pytest`: wrong password rejected with a generic message; correct login issues a working session cookie; every existing protected route 401s with no cookie and works with one; logout actually invalidates (a reused cookie post-logout gets 401); the bootstrap admin is created exactly once even across repeated startups.
- Manual: log in via the real frontend, confirm the login screen gates access, refresh the page and confirm the session persists (cookie survives), log out and confirm the login screen reappears and API calls 401.

## Sizing

M (roughly the same order as phases 2/4/5 — new subsystem, but no new external service dependency since sessions are in-memory).

## Status: Code complete, fully verified (no external dependency to block on)

Unlike phases 3–6, this phase needed no `ANTHROPIC_API_KEY` and nothing here is gated on it — everything below has been exercised for real.

- `app/services/auth_service.py` — `UserRecord`/`SessionRecord`, `bcrypt` hashing/verification, `AuthStoreProtocol`/`InMemoryAuthStore` (same DI-seam pattern as `store.py`).
- `app/routers/auth.py` — `POST /auth/login` (generic "Invalid username or password" whether the username is wrong or the password is — no enumeration), `POST /auth/logout`, `GET /auth/me`, `POST /auth/register` (requires an existing session, no open signup). `get_current_user` is the shared dependency every other router now requires.
- Session delivery: an `HttpOnly`, `SameSite=Lax` cookie, `Secure` gated behind `SESSION_COOKIE_SECURE` (off by default so local `http://localhost` dev keeps working — a `Secure` cookie is silently dropped by the browser over plain HTTP).
- Every existing router — `chat`, `test_runs`, `test_cases`, `reports` — now declares `dependencies=[Depends(get_current_user)]` at the router level, so no individual endpoint signature needed touching. `websockets.py` couldn't use that pattern (raising `HTTPException` from a WS-scoped dependency doesn't reliably close the handshake across FastAPI/Starlette versions) — it manually checks `websocket.cookies.get("session")` and closes with code 4401, mirroring the unknown-run-id 4004 pattern already in that file.
- Bootstrap: `app/main.py` moved off the deprecated `@app.on_event("startup")` onto a `lifespan` context manager while adding this; `_bootstrap_seed_admin()` creates exactly one account from `INITIAL_ADMIN_USERNAME`/`PASSWORD` if the user store is empty, no-ops otherwise.
- Frontend: `useAuth.js` (checks `GET /auth/me` on load — this is what makes a session survive a page refresh), `LoginScreen.jsx`, a sign-out affordance in the chat sidebar header. `App.jsx` gates all existing UI behind `useAuth`. Every `api.js` call now goes through a shared `apiFetch()` wrapper adding `credentials: "include"`, required for the cookie to actually cross the `localhost:5173` → `localhost:8000` port boundary in local dev.
- **Test strategy note:** rather than editing every pre-existing router test to authenticate individually, `tests/conftest.py`'s `client` fixture now logs in as a fresh per-test user before handing back the `TestClient` — the entire phase 0–6 test suite (chat, test runs, reports, websockets, everything) kept working completely unmodified against the new auth requirement. A separate `unauthenticated_client` fixture exists specifically for tests that need to exercise the logged-out path.
- 12 new tests (`test_auth.py`, `test_websockets.py` gained one more): login success issues a working session validated by a follow-up `/auth/me` call; wrong password and unknown username both get the identical generic message; every protected REST route 401s logged-out and 200s logged-in (looped over `/test-cases`, `/test-runs`, `/chat/history`); the WS logs endpoint 401s (close code 4401) without a session; logout actually invalidates (reusing the cookie afterward 401s, not a no-op flag flip); register requires a session and rejects duplicate usernames; password hashing round-trips correctly and never stores/compares plaintext; the bootstrap admin is created exactly once even when startup logic runs three times in a row, and does nothing when no seed credentials are configured. Full backend suite: 94 passed, 1 skipped (the unrelated phase 3 live-API integration test). Frontend: `vitest run` 3/3, `vite build` clean.
- **Manually verifiable right now** (unlike phases 3–6, nothing here needs the Claude key): set `INITIAL_ADMIN_USERNAME`/`PASSWORD` in `backend/.env`, start the backend, load the frontend — it should show the login screen, reject a wrong password, accept the right one, survive a page refresh, and sign-out should kick back to the login screen with API calls then 401ing.

**What's not yet done:** the manual walkthrough above hasn't actually been run against a live browser session in this environment — only the automated test suite (which drives the same code paths via `TestClient`, including a real WS handshake and a real bcrypt hash/verify round-trip) has been exercised. Rate limiting on login attempts (F-010) and per-user data isolation remain explicitly out of scope, as decided.
