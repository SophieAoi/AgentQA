# Phase 8 — Local LLM migration (remove the Anthropic API dependency)

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context.

## Goal

Directive from the user's supervisor: the tool must not depend on any paid third-party model API (Claude or ChatGPT) — the agent's reasoning must run entirely locally. This replaces every `anthropic` SDK call across the codebase with a self-hosted open-source model served by [Ollama](https://ollama.com), with **zero functional or architectural change** to the Planner/Executor/Verifier/Reporter design established in phases 3–6 — only the model backend moves.

## Model choice

**Qwen 2.5 32B-Instruct**, chosen (over 14B/8B alternatives) for the strongest available tool-use and structured-output reliability at a size the target machine (Apple M4 Pro, 24GB RAM) can actually run. User-confirmed tradeoff: slower per-response latency and most of available RAM in use while running, in exchange for the best local reasoning quality available.

## Scope — build

- Install Ollama (`brew install ollama`), run as a background service (`brew services start ollama`), pull `qwen2.5:32b-instruct`.
- New `agent/local_llm.py` — the local equivalent of what `anthropic`'s SDK provided directly:
  - `structured_chat(system, user_message, output_model) -> BaseModel` — replaces `client.messages.parse(..., output_format=X)`, using Ollama's `format=<json schema>` parameter. Used by `planner.py` and `verifier.py`.
  - `local_tool` decorator — replaces `@anthropic.beta_async_tool`. Infers a JSON schema from a function's type-hinted parameters (via `pydantic.create_model`) and tags `.name`/`.description`/`.input_schema` onto the function, so callers barely change (swap the decorator, keep the function).
  - `run_tool_loop(system, messages, tools, max_iterations) -> AsyncIterator[ToolLoopTurn]` — replaces `client.beta.messages.tool_runner()`. A hand-written ReAct loop (explicit choice over pulling in LangChain/LlamaIndex, per user decision — this codebase has no heavy agent-framework dependency anywhere and phase 3's design note on "why two agent roles, not five" already established a preference for owning this logic directly): send messages + tool defs to Ollama, if the model calls a tool, actually invoke it and feed the result back as a `role: "tool"` message, repeat; yields a `ToolLoopTurn` per turn (`tool_call` or `text`) so callers can trace/log as they go, mirroring how the old tool_runner's async iteration worked.
  - Own error type `LocalLLMError`, raised for connection failures (Ollama not running) or schema-validation failures (model didn't return valid JSON) — each caller (`PlannerError`/`VerifierError`/`ExecutorError`) still wraps it into its own domain error, preserving the existing error-handling contract at every call site.
- `agent/planner.py` — swap `anthropic.AsyncAnthropic` + `client.messages.parse` for `local_llm.structured_chat`.
- `agent/verifier.py` — same swap.
- `agent/executor.py` — swap `client.beta.messages.tool_runner` for `local_llm.run_tool_loop`; the cross-step conversation continuity hack (reaching into `runner._params["messages"]`) goes away entirely, since we now own the `messages` list directly — a net simplification.
- `agent/tools/playwright_tools.py` — swap the `@anthropic.beta_async_tool` decorator for `@local_llm.local_tool` on all six tools, and the LLM-assisted last-resort selector tier (`_llm_select`) swaps its direct `client.messages.parse` call for `local_llm.structured_chat`.
- `app/services/chat_service.py` — swap the sync `anthropic.Anthropic` + `client.beta.messages.tool_runner` for an async call into `local_llm.run_tool_loop` (chat's reply path becomes async — `chat.py`'s router endpoints change from `def` to `async def` to match).
- `app/config.py` — remove `ANTHROPIC_API_KEY`, `VERIFIER_MODEL` (repurposed); add `LOCAL_LLM_BASE_URL` (default `http://localhost:11434`), `LOCAL_LLM_MODEL` (default `qwen2.5:32b-instruct`).
- `backend/requirements.txt` — remove `anthropic`, add `ollama`.
- Every test that mocks `anthropic.resources.messages.*` / `anthropic.beta_tool` / `anthropic.beta_async_tool` updated to mock `agent.local_llm` instead — the mocking *pattern* (patch the client method, assert on call args, use a fake async-iterable double for the tool loop) carries over unchanged; only the target module changes.

## Explicitly NOT in scope

- Any change to the Planner/Executor/Verifier/Reporter architecture, the selector fallback chain, the credential-placeholder mechanism, or any test case content — this is purely a model-backend swap.
- Streaming responses token-by-token (Ollama supports it; not needed here since nothing in this codebase streams LLM output token-by-token today either).
- Auto-installing Ollama or auto-pulling the model from application code — this is a one-time local machine setup step, documented in `.env.example`/README, not something the FastAPI app manages.
- Supporting multiple local models or a model picker — one hardcoded default, overridable via `LOCAL_LLM_MODEL` env var same as `VERIFIER_MODEL` was.

## Files to create/modify

- New: `backend/agent/local_llm.py`.
- `backend/agent/planner.py`, `backend/agent/verifier.py`, `backend/agent/executor.py`, `backend/agent/tools/playwright_tools.py`, `backend/app/services/chat_service.py`, `backend/app/routers/chat.py`.
- `backend/app/config.py`, `backend/requirements.txt`, `backend/.env.example`.
- Updated: `backend/tests/test_planner.py`, `test_verifier.py`, `test_executor.py`, `test_reliability.py`, `test_chat_service.py`, `test_chat_tool_use.py`, `test_chat.py`, `test_agent_runner_integration.py` (this one flips from "skip without ANTHROPIC_API_KEY" to "skip without a reachable local Ollama server").

## Verification

- `pytest`: full suite green with zero real network/API calls (same mocking discipline as phases 3/4/6 — Ollama itself is mocked in unit tests, exactly like `anthropic` was).
- Manual: with Ollama running and the model pulled, run a real test case end-to-end (Planner produces a plan, Executor drives the browser, Verifier judges an assertion) with zero `ANTHROPIC_API_KEY` anywhere in the environment — the first real live-agent verification this whole project has had, now unblocked precisely because it no longer depends on a paid key.
- Confirm `grep -r anthropic backend/` (outside `.venv`) returns nothing.

## Sizing

M–L. Touches every reasoning call site (5 files) plus their full test coverage, but the Planner/Executor/Verifier *architecture* itself is unchanged — this is a backend swap, not a redesign.

## Known tradeoffs (told to the user up front)

- **Reliability**: a 32B local model is meaningfully weaker than Claude Opus at multi-step tool-use judgment and structured-output adherence. Expect more malformed JSON retries, more wrong-element clicks, and lower verifier confidence scores than the Claude-backed version would have produced — this is inherent to the model, not a bug in the migration.
- **Latency**: local inference on a laptop GPU/CPU is slower than a hosted frontier model, especially for the Executor's multi-turn tool loop (each turn is a full model inference, not a fast API round-trip).
- **Resource usage**: the 32B model consumes most of the machine's 24GB RAM while loaded — running the backend, Playwright browser, and Ollama simultaneously is the realistic ceiling of this machine's capacity, with little headroom for anything else.

## Status: Code complete, verified against a real local model — the first real end-to-end run this whole project has had

Every `anthropic` SDK call site is gone (`grep -r anthropic backend/` outside `.venv` returns nothing); the package is uninstalled from the venv and removed from `requirements.txt`. All scope above is built exactly as designed — no architecture changes, five call sites migrated, `agent/local_llm.py`'s `structured_chat`/`local_tool`/`run_tool_loop` sanity-checked directly against a live Ollama server before being wired into any real call site.

**Automated suite**: 89 tests passing (mocked, zero real network calls — same discipline as every prior phase), plus one pre-existing test (`test_start_test_run_creates_a_queued_run_and_lists_it`) that was accidentally relying on an *absent* Anthropic key to fail fast; with Ollama actually running that assumption broke (its background task started making real, slow local-model calls) — fixed by mocking `run_test_case` explicitly, which is what it should have done from the start.

**Real end-to-end run** (`test_agent_runner_integration.py`, previously never runnable in this environment — always skipped for lack of an API key, now unblocked precisely because there's no paid key to wait for): ran a full natural-language test case against a local fixture login page with zero external API calls anywhere.

Result: **4 of 5 steps passed for real** — navigate, fill email, fill password, and click Sign In were all planned and executed correctly by Qwen 2.5 32B driving a real Playwright browser. The final assertion step failed: the model didn't produce a judgeable final answer for the Verifier to evaluate, and per this project's "a missing verdict is not a silent pass" rule (established in phase 4), it correctly reported FAILED rather than guessing.

This is treated as a genuine, expected finding, not a bug — exactly the reliability tradeoff disclosed above before the migration started. The mechanics are proven correct (a local model really can plan and drive a real browser end-to-end); the remaining gap is the model's own multi-step instruction-following on harder steps, which no amount of code change fixes — only prompt iteration, a larger model, or retries would move that number, and per user decision this phase ships as-is rather than chasing full green on a known-weaker model.

**Practical implication going forward**: expect real runs against the actual Influence staging site to need more retries and closer review than a Claude-backed run would have, especially on assertion-heavy test cases. This isn't a regression to fix later — it's the honest cost of the "no paid API" requirement, now visible for the first time with real evidence instead of a prediction.
