# AgentQA

An autonomous QA agent that tests [Influence](https://influence-stg.movingwalls.com), a DOOH/OOH advertising campaign management platform, by driving a real browser the way a human tester would — reading a test case written in plain English, planning the steps, executing them with Playwright, and judging the result.

Runs entirely on a local LLM (Ollama) by default — no paid API required.

## How it works

Three roles, one loop per test case:

1. **Planner** (`agent/planner.py`) — one structured-output LLM call turns a test case's natural-language description into an ordered list of concrete steps (navigate, click, fill, select, assert).
2. **Executor** (`agent/executor.py`) — a tool-calling loop that walks the plan with a small Playwright tool set. After every tool call it gets the result (DOM/accessibility snapshot, screenshot, error) back before deciding the next action.
3. **Verifier** (`agent/verifier.py`) — judges each assertion step against the page's actual state. Verification is flakiness-gated majority voting (`agent/flakiness_tracker.py`): a case with a history of split verdicts gets re-judged multiple times before a result is trusted, instead of accepting the first judgment call.

Results, screenshots, and step-by-step traces are streamed live over WebSockets and viewable in the frontend; a static HTML/PDF report is generated per run (`agent/reporter.py`).

Test cases that genuinely can't be automated in this environment (no ad-serving pipeline, no physical DOOH player, missing media fixtures, etc.) are tagged with an explicit `GAP: <reason>` precondition rather than being deleted or left to fail — see `docs/AgentQA_Problems_and_Solutions_Plan.docx` for the full breakdown.

## Project layout

```
backend/
  app/            FastAPI app — routers (auth, chat, test runs, test cases, reports, websockets), models, services
  agent/          Planner / Executor / Verifier, Playwright tool set, browser session, test case YAML files
  tests/          pytest suite
frontend/         React + Vite single-page app (chat-driven run trigger, live log/screenshot viewer, test case tracker)
docs/             Phased build plan, audits, and problem/solution writeups
```

## Prerequisites

- Python 3.11+
- Node 18+
- [Ollama](https://ollama.com), running locally with a pulled model:
  ```bash
  ollama pull qwen2.5:14b-instruct
  ollama serve
  ```
- Playwright browsers (installed below)

## Setup

### Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
playwright install chromium
cp .env.example .env   # then fill in the values below
```

Required environment variables (`backend/.env`):

| Variable | Purpose |
|---|---|
| `INFLUENCE_TEST_USERNAME` / `INFLUENCE_TEST_PASSWORD` | Staging login credentials the agent drives the browser with |
| `INFLUENCE_TEST_CAMPAIGN_ID` | A live, known-good campaign/deal used as a stable fixture by cases needing "an existing deal" |
| `INITIAL_ADMIN_USERNAME` / `INITIAL_ADMIN_PASSWORD` | Seed admin account for this app's own login, created once on first startup |

Everything else has a sane default (local Ollama URL/model, headless browser, 900s per-case timeout) — see `backend/app/config.py` for the full list and the reasoning behind each default.

Optional cloud backends (`USE_NVIDIA_BACKEND`, `USE_GEMINI_BACKEND`) exist for A/B speed comparisons but are off by default — real runs never silently leave the local model.

Run the backend:

```bash
uvicorn app.main:app --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

## Testing

```bash
cd backend
.venv/bin/pytest

cd frontend
npm test
```

## Docs

- [`docs/BUILD-PLAN.md`](docs/BUILD-PLAN.md) — phased build history and shared design decisions (why two agent roles, self-healing selector fallback chain, streaming architecture, data model).
- [`docs/AgentQA_Problems_and_Solutions_Plan.docx`](docs/AgentQA_Problems_and_Solutions_Plan.docx) — open problems (verifier judgment consistency, workflow friction, local hardware speed, stale test-case content, test-case authoring scale) and their status.
- [`docs/AgentQA_Progress_Summary.docx`](docs/AgentQA_Progress_Summary.docx) — progress summary.
