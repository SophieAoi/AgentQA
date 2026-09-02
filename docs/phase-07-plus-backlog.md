# Phase 7+ — Backlog

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context. These items are named for scope tracking, not designed in detail — each should get its own phase doc (following the same template as phases 0–6) when it's actually prioritized.

- **Postgres/SQLAlchemy migration.** The `ExecutionStep`/`AgentTrace` schema (defined in phase 3) is deliberately designed to make this a column-type swap, not a redesign — string FKs already match the `run_id: str` pattern SQLAlchemy will formalize.
- **Redis.** Backs a multi-worker `StoreProtocol` implementation and phase 5's event bus at scale (replacing the in-process `asyncio.Queue` pub/sub).
- **Docker / deployment.** Dockerfiles + `docker-compose.yml` once there's a real multi-service topology (Postgres, Redis, backend, frontend) worth containerizing together.
- **Real authentication/authorization.** Blocks any non-localhost deployment — this is audit finding F-004, currently harmless (localhost-only, single-user) but a hard blocker the moment a second user or non-loopback network is involved.
- **Dashboard / project / test-suite CRUD UI.** The original spec's "Create Project" / "Create Test Suite" / version history flows — layered on top of the agent engine once it's proven, not before.
- **Cross-browser (Firefox/WebKit) and mobile-viewport execution.**
- **Visual regression testing.**
- **Video recording / session replay.**
- **Accessibility (axe-core) and performance (Lighthouse) testing** as additional automated checks per run.
- **Slack / Jira / GitHub / email integrations** for notifications and triage.
- **Full LLM-based DOM-selector inference** as a primary (not last-resort) strategy — only worth building if phase 3's fallback-chain telemetry (`selector_strategy` distribution across real runs) shows the mechanical tiers aren't covering enough cases.
- **Scheduled / CI-triggered runs.**
- **A cheap-model live "narrator"** — a separate, possibly Haiku-tier model subscribed to phase 5's event stream purely for live commentary, distinct from the Executor's own reasoning.
- **Full Verifier/Observer agent-process split**, if cross-model verification or true multi-agent delegation (separate subagent threads) becomes a real requirement — the phase 3/4 data model already carries what a split-out Verifier would need (`expected_outcome` / `actual_result` on every `ExecutionStep`), so this is a responsibility refactor later, not a schema change.
