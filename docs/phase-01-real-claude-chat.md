# Phase 1 — Real Claude wiring for chat

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context and design rationale.

## Goal

Replace the `reply_to()` string-matching stub with a real Claude call, proving the Anthropic SDK integration and API-key/env plumbing work in isolation — before wiring Claude into the much more complex agent loop (phase 3).

## Scope — build

- Add `anthropic` to `requirements.txt` (pinned).
- `ChatService.reply_to()` (extracted in phase 0) becomes a real call: `client.messages.create(model=..., max_tokens=1024, system=<QA-agent persona>, messages=[...])`. No tools, no extended thinking — this is a single-call chat, not an agent.
- Load `ANTHROPIC_API_KEY` from environment via a new `backend/app/config.py` (centralizes env loading — reused by phase 2's CORS-from-env and later config needs). `.env` must be gitignored.
- Chat history sent as real multi-turn context, built from `store.get_chat_history()`, capped at the last N turns to bound token usage (this doubles as a first pass at F-010's input-limit concern).
- Typed error handling: catch `anthropic.APIStatusError` / `RateLimitError` / `APIConnectionError` distinctly and return a graceful chat-visible error message rather than a 500.

## Explicitly NOT in scope

- Tool use / function calling (phase 6 adds exactly one tool, `start_test_run`).
- Playwright or test-runner integration via chat — "run TC-001 for me" stays out until phase 6.
- Streaming chat responses (batched with phase 5's WebSocket work).

## Files to create/modify

- `backend/requirements.txt` — add `anthropic==`.
- `backend/app/services/chat_service.py` — real implementation.
- `backend/app/config.py` — new; centralized env loading.
- `backend/.env.example` — new; documents `ANTHROPIC_API_KEY`.
- `backend/tests/test_chat_service.py` — mock the Anthropic client; assert prompt construction and error-path behavior.

## Verification

- Manual: send a message via the existing chat UI, get a real Claude reply, confirm multi-turn context works (ask a follow-up referencing the prior message).
- `pytest` with a mocked `anthropic.Anthropic` client (`unittest.mock.patch` on `client.messages.create`) — no live API calls in the default test suite. Optionally one `@pytest.mark.integration` test gated behind an env var for manual/CI-with-secret runs.
- Manual: temporarily set an invalid API key, confirm the chat UI shows a readable error instead of crashing or hanging.

## Sizing

XS–S (1–2 days). Low risk, isolated to one service.
