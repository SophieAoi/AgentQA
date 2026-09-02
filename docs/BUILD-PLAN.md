# AI QA Automation Platform — Build Plan Index

This is the index for an incremental, phase-by-phase build of the AI QA agent platform on top of the existing AgentQA skeleton. Each phase has its own doc (linked below), is independently shippable, and is verified before the next phase starts. Nothing here replaces the existing codebase — every phase builds on top of it.

Background: an enterprise audit was run on the pre-existing skeleton and is recorded at [`enterprise-review-2026-07-24.md`](enterprise-review-2026-07-24.md) (20 findings, plus `architecture-review-2026-07-24.md` and `security-audit-fullstack-2026-07-24.md`). Phase 0 below **is** that audit's queued fixes plus its proposed ADR-001 (StoreProtocol DI seam), done first because the agent engine is about to add substantially more state on top of `backend/app/services/store.py`.

## Phases

| Phase | Doc | Goal | Sizing |
|---|---|---|---|
| 0 | [phase-00-audit-fixes-and-store-di.md](phase-00-audit-fixes-and-store-di.md) | Apply audit quick-fixes + StoreProtocol DI seam; stand up first test infra | S–M |
| 1 | [phase-01-real-claude-chat.md](phase-01-real-claude-chat.md) | Real Claude call for chat (no tools/agent loop yet) | XS–S |
| 2 | [phase-02-playwright-skeleton.md](phase-02-playwright-skeleton.md) | Real Playwright execution, hardcoded steps, no LLM | M |
| 3 | [phase-03-planner-executor-agent-loop.md](phase-03-planner-executor-agent-loop.md) | Planner + Executor agent loop (core deliverable) | L |
| 4 | [phase-04-verifier-and-reliability.md](phase-04-verifier-and-reliability.md) | Formal verifier, retries, timeouts, confidence scoring | M |
| 5 | [phase-05-live-streaming-websockets.md](phase-05-live-streaming-websockets.md) | WebSocket live logs + screenshot streaming | M |
| 6 | [phase-06-reporting-and-chat-trigger.md](phase-06-reporting-and-chat-trigger.md) | HTML/PDF reports, chat-triggered runs | M |
| 7+ | [phase-07-plus-backlog.md](phase-07-plus-backlog.md) | Named backlog (Postgres, auth, Docker, integrations, etc.) | — |
| 9 | [phase-09-flakiness-gated-majority-voting.md](phase-09-flakiness-gated-majority-voting.md) | Fix verifier judgment non-determinism via gated majority voting | S–M |
| 10 | [phase-10-stale-campaign-fixture-and-line-item-flow.md](phase-10-stale-campaign-fixture-and-line-item-flow.md) | Fix stale campaign fixture ID + map/document the real Line Item creation form | S–M |
| 11 | [phase-11-compound-step-fill-and-dashboard-url-fix.md](phase-11-compound-step-fill-and-dashboard-url-fix.md) | Fix compound-step fill truncation + Dashboard URL over-fixation | XS–S |
| 12 | [phase-12-planner-invented-assertion-steps.md](phase-12-planner-invented-assertion-steps.md) | Stop Planner from inventing unrequested assertion/wait steps | XS |
| 13 | [phase-13-post-navigation-settle-race.md](phase-13-post-navigation-settle-race.md) | Fix post-navigation settle race in click() (measured live) | S |
| 14 | [phase-14-blank-field-steps-and-merged-text-bias.md](phase-14-blank-field-steps-and-merged-text-bias.md) | Fix blank-field no-op steps + merged-text verifier bias (root-caused in the snapshot itself) | M |
| 15 | [phase-15-test-case-crud-ui.md](phase-15-test-case-crud-ui.md) | Create/edit/delete test cases from the UI, writing straight through to agent/test_cases/*.yaml | M |

## Shared design decisions

These apply across multiple phases and are recorded once here rather than repeated in every phase doc.

### Why two agent roles (Planner + Executor), not five

The original spec described five agents: Planner, Executor, Observer, Verifier, Reporter. This plan builds two initially:

- **Planner** — a single structured-output Claude call that turns a natural-language test case into an ordered list of `PlannedStep`s. Not a persistent process — one call per test case.
- **Executor** — a ReAct-style tool-calling loop (Claude + a small Playwright tool set) that walks the plan. After every tool call, Claude receives the tool's result (DOM state, screenshot, error) back in its own context before deciding the next action. **That tool-result turn is the observation step** — a separate "Observer" agent would just duplicate context Claude already has.

Verification and reporting are **not** separate agents from day one either:
- Verification is the last step of each planned step — an `assert_condition` tool call followed by a judgment over its result. Phase 4 promotes this into a dedicated `agent/verifier.py`, but only once the pattern is proven inside the Executor loop, not invented as a third agent up front.
- Reporting is **deterministic templating** (phase 6, `agent/reporter.py`), not an LLM call at all — turning structured `ExecutionStep` data into HTML/PDF needs no reasoning.

When a real split pays for itself: a genuinely different/cheaper model doing verification (phase 4 leaves this as a config knob), or a live narrator model watching the event stream for phase-5-style commentary (backlog). Track these as upgrades, not day-one requirements.

### Self-healing selectors: mechanical fallback chain first

Phase 3 ships a deterministic fallback chain, not a per-broken-selector LLM call:

1. Primary selector (Playwright's own `getByRole`/`getByText`/`getByTestId` — already fairly resilient).
2. Mechanical fallbacks in order: (a) same element by accessible name/role ignoring exact text, (b) fuzzy substring text match, (c) cached last-known-good selector for this step from a prior successful run.
3. Only if **all** mechanical tiers fail does the Executor make one extra Claude call: pass the current DOM/accessibility-tree snapshot + the original intent + the previous known-good selector, ask it to pick a candidate or report not-found.

Every tier actually used is logged (`ExecutionStep.selector_strategy`) so real breakage-rate telemetry — not a guess — decides whether investing in fuller LLM-based DOM analysis (backlog) is ever worthwhile.

### Streaming comes after the agent loop works headlessly

Phases 0–4 are fully headless: screenshots to disk, structured logs/steps written synchronously to the store, debuggable via the existing polling `GET /test-runs/{id}` endpoint and a new `GET /test-runs/{id}/trace` endpoint. WebSockets (`WS /ws/test-runs/{id}/logs`, `WS /ws/test-runs/{id}/browser`) are introduced in phase 5, additively — polling stays as the fallback/reconnect-reconciliation path. This means agent-logic bugs and transport bugs are never being debugged at the same time.

The `/browser` socket pushes periodic **screenshot frames**, not a live embedded DOM/iframe — `frontend/src/components/SiteViewer.jsx` already documents why true iframe embedding doesn't work here (the target site's OAuth redirect + browsers blocking third-party cookies in iframes hangs the flow indefinitely). Streaming screenshots sidesteps that same constraint rather than re-fighting it.

### Data model, designed once (introduced in Phase 3, stable through the future Postgres migration)

Defined in `backend/app/models/schemas.py`, added when the Executor first needs them (phase 3):

```python
class SelectorStrategy(str, Enum):
    primary = "primary"
    fallback_role = "fallback_role"
    fallback_fuzzy = "fallback_fuzzy"
    fallback_cache = "fallback_cache"
    llm_selected = "llm_selected"

class StepType(str, Enum):
    navigate = "navigate"
    action = "action"            # click, fill, select, etc.
    assertion = "assertion"      # the verifier-flavored step
    observation = "observation"  # read-only DOM/screenshot capture, no page mutation

class ExecutionStep(BaseModel):
    id: str                                  # uuid, PK once in Postgres
    run_id: str                              # FK -> TestRunDetail.run_id
    test_case_id: str
    step_index: int
    step_type: StepType
    intent: str                              # planner's natural-language description
    tool_name: Optional[str] = None
    tool_input: Optional[dict] = None
    selector_used: Optional[str] = None
    selector_strategy: Optional[SelectorStrategy] = None
    expected_outcome: Optional[str] = None
    actual_result: Optional[str] = None
    status: TestRunStatus                    # reuse queued/running/passed/failed/error
    screenshot_url: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    error_detail: Optional[str] = None

class AgentTrace(BaseModel):
    id: str
    run_id: str
    step_id: Optional[str] = None            # nullable: planning entries precede any step
    role: Literal["planner", "executor", "verifier"]
    message_type: Literal["reasoning", "tool_call", "tool_result", "final_answer"]
    content: str
    token_usage_input: Optional[int] = None
    token_usage_output: Optional[int] = None
    created_at: datetime
```

`run_id`/`step_id` are plain strings from day one, matching the existing `run_id: str` UUID-slice pattern in `store.py` — so the eventual SQLAlchemy `ForeignKey` swap is a column-type change, not a schema redesign. `TestRunDetail.steps: list[TestStepResult]` (today's frontend-facing summary model) stays as-is; `ExecutionStep`/`AgentTrace` are the new internal detail record — the frontend never needs full tool-call JSON.

## How to use these docs

Each phase doc is self-contained: goal, exact scope (build vs. explicitly not-in-scope), file paths to create/modify, and how to verify the phase before starting the next one. A fresh session should be able to open a single phase doc and start implementing without needing this index, beyond the shared decisions above.
