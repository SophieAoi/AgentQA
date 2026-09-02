# Phase 3 — Planner + Executor agent loop (core deliverable)

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context and design rationale — especially "Why two agent roles" and "Self-healing selectors," both directly implemented here.

## Goal

Replace phase 2's hardcoded action sequence with a real Planner (Claude turns natural-language test case text into a structured step plan) + Executor (Claude, via tool use, drives Playwright through that plan). This is the heart of the whole project.

## Scope — build

- Introduce `ExecutionStep`, `AgentTrace`, `PlannedStep`, `SelectorStrategy`, `StepType` into `schemas.py` (full shapes in [BUILD-PLAN.md](BUILD-PLAN.md#data-model-designed-once-introduced-in-phase-3-stable-through-the-future-postgres-migration)). Extend `StoreProtocol` with `add_execution_step`, `add_agent_trace`, `get_execution_steps(run_id)`; `InMemoryStore` gets corresponding dict-of-lists storage.
- `agent/planner.py` — one Claude call with structured output (JSON schema), taking the test case's natural-language `description` + a lightweight page-context hint, returning an ordered `PlannedStep` list (`step_index`, `intent`, `step_type`, `expected_outcome`).
- `agent/executor.py` — a ReAct tool-use loop (Claude's tool-runner pattern) over a small, explicit Playwright tool set — not a raw shell escape hatch:
  - `click(selector_hint: str)`, `fill(selector_hint: str, value: str)`, `select_option(selector_hint: str, value: str)`, `navigate(url: str)`, `read_page(scope_hint: Optional[str])` (returns a trimmed accessibility-tree/DOM snapshot, not raw HTML — keep tool-result tokens bounded), `assert_condition(description: str)` (the built-in verification action — tool result is DOM state, the next Claude turn makes the pass/fail judgment).
  - Each tool resolves its `selector_hint` into a real Playwright locator via the fallback chain in `agent/tools/selector_resolver.py`; logs which `SelectorStrategy` tier resolved it.
  - Every tool call + result is written to `AgentTrace` (`role=executor`) as it happens; each planned step's execution produces one `ExecutionStep` row.
  - Wrap the per-test-case run in try/except (same precedent as phase 0's F-005 fix) — on unhandled exception, mark remaining unexecuted steps `status=error` and the run overall `error`.
- `agent/runner.py::run_test_case()` — now: Planner call → for each `PlannedStep`, feed to the Executor loop → collect `ExecutionStep`s → aggregate into the existing `TestStepResult`/`TestRunDetail` summary shape (frontend contract from phases 0–2 stays unchanged; new detail is available via a new endpoint, not forced onto the existing one).
- New endpoint: `GET /test-runs/{run_id}/trace` → `list[AgentTrace]`, for debugging full agent reasoning/tool calls without touching the existing frontend UI yet.

## Explicitly NOT in scope

- Streaming any of this to the frontend live — still headless; the polling UI sees only the final result, same as phase 2, just backed by real reasoning (phase 5 adds streaming).
- A separate Verifier agent process — the `assert_condition` tool call + Claude's next-turn judgment **is** the verification; phase 4 formalizes and hardens this, it doesn't invent it from scratch.
- Cross-browser/mobile, video.
- LLM-based full-DOM selector inference as the *first* resort — only the last-resort fallback tier (see [BUILD-PLAN.md](BUILD-PLAN.md#self-healing-selectors-mechanical-fallback-chain-first)).

## Files to create/modify

- `backend/app/models/schemas.py` — add `ExecutionStep`, `AgentTrace`, `SelectorStrategy`, `StepType`, `PlannedStep`.
- `backend/app/services/store.py` — extend `StoreProtocol` + `InMemoryStore`.
- New: `agent/planner.py`, `agent/executor.py`, `agent/tools/playwright_tools.py`, `agent/tools/selector_resolver.py`.
- `agent/runner.py` — rewire to Planner → Executor.
- New: `backend/app/routers/traces.py` (or extend `test_runs.py`) — `GET /test-runs/{run_id}/trace`.
- `backend/tests/test_planner.py` — mock Claude, assert structured-output parsing/schema validation.
- `backend/tests/test_executor.py` — mock Claude tool-calls and Playwright, assert the fallback chain tries tiers in order and logs the right `selector_strategy`.
- `backend/tests/test_agent_runner_integration.py` — end-to-end against phase 2's local fixture page, asserting a full run produces the expected `ExecutionStep` sequence and final status.

## Verification

- `pytest` unit coverage on Planner (schema-valid output across a range of test-case descriptions) and Executor (tool loop terminates, fallback chain order, error handling) with mocks.
- One real integration test against the fixture page (or, gated behind a manual/CI-secret flag, real staging) proving a full natural-language test case executes correctly end-to-end with a live Claude call.
- Manual: run TC-001..004 through the new pipeline, compare pass/fail outcomes and step-by-step reasoning (via `GET /test-runs/{id}/trace`) against what a human tester would expect.
- Log `usage.input_tokens`/`output_tokens` per `AgentTrace` row so cost-per-run can be eyeballed before scaling usage.

## Sizing

L (1.5–2.5 weeks). Largest, highest-risk phase — Planner/Executor design, tool set, and selector-resolution logic are all new and will need iteration against real pages.

## Status: Code complete, live verification pending an API key

All scope above is built:

- `agent/planner.py` — structured-output call via `client.messages.parse(output_format=PlannedStepList)`.
- `agent/tools/selector_resolver.py` — 4-tier mechanical fallback chain (primary → fallback_role → fallback_fuzzy → fallback_cache), each tier tested individually against a real browser fixture (`tests/test_selector_resolver.py`, 6/6 passing) to confirm they actually discriminate rather than all accidentally matching the same element.
- `agent/tools/playwright_tools.py` — the six-tool set (`click`/`fill`/`select_option`/`navigate`/`read_page`/`assert_condition`), each call recorded as an `ExecutionStep`; last-resort `llm_selected` tier makes one extra Claude call with an `aria_snapshot()` page snapshot when every mechanical tier fails.
- `agent/executor.py` — per-planned-step tool-use loop via `client.beta.messages.tool_runner`, cross-step conversation continuity, `AgentTrace` logging (reasoning/tool_call/final_answer), and a `RESULT: PASS`/`RESULT: FAILED` marker convention for assertion-step verdicts (a missing marker defaults to FAILED, never a silent pass).
- `agent/runner.py` rewired: login stays Phase 2's hardcoded, already-verified precondition step (not something re-planned every run); Planner → Executor now drives the actual test logic.
- `GET /test-runs/{run_id}/trace` endpoint.
- 15 new tests (`test_planner.py`, `test_selector_resolver.py`, `test_executor.py`) — all mocked/fixture-based, no live API calls, all passing. `test_agent_runner_integration.py` (the one test meant to make a real Claude call end-to-end against the fixture login page) is written and correctly skips itself via `pytest.mark.skipif` when `ANTHROPIC_API_KEY` is unset — confirmed skipping cleanly, not erroring.

**What's not yet done:** no `ANTHROPIC_API_KEY` was available in this environment, so none of this has been exercised against a real Claude call — the fixture integration test has never actually run (only confirmed it skips correctly), and TC-001–004 haven't been run through the new pipeline against real staging. Once a key is added to `backend/.env`, next steps are exactly the doc's Verification section: run the fixture integration test for real, then TC-002/TC-003 (which don't depend on the known inventory gap from Phase 2) against real staging, comparing the agent's own plan and reasoning (via `GET /test-runs/{id}/trace`) against what a human tester would expect — this is the first time the Planner's actual plan quality and the selector fallback chain's real-world hit rate can be observed, not just unit-tested in isolation.
