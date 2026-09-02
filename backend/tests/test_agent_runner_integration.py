"""
Real, end-to-end integration test: a live local-model Planner call, a live
local-model+Playwright Executor tool-use loop, against the local two-origin
login fixture (not the real staging site — see
docs/phase-03-planner-executor-agent-loop.md verification section). Skipped
automatically when no local Ollama server is reachable, so it never blocks
the default (mocked) test suite or CI runs without one set up — this is the
one test in the suite that costs real wall-clock time (a real local-model
inference is much slower than a mocked response).
"""

import socket

import pytest

from app.config import LOCAL_LLM_BASE_URL
from app.services.event_bus import EventBus
from app.services.store import InMemoryStore
from agent.browser.session import BrowserSession
from agent.executor import run_executor
from agent.planner import plan_test_case


def _ollama_reachable() -> bool:
    try:
        host_port = LOCAL_LLM_BASE_URL.split("://", 1)[-1]
        host, _, port = host_port.partition(":")
        with socket.create_connection((host, int(port or 11434)), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _ollama_reachable(),
    reason=f"No local Ollama server reachable at {LOCAL_LLM_BASE_URL} — this test makes real local-model calls.",
)


async def test_full_planner_executor_loop_against_fixture_login(two_origin_fixture_servers):
    app_origin, _login_origin = two_origin_fixture_servers
    store = InMemoryStore()
    event_bus = EventBus()
    run = store.create_test_run(["TC-INTEGRATION"])

    description = (
        f"Navigate to {app_origin}. Fill the 'Email / Username' field with "
        f"fixture@example.com and the 'Password' field with fixture-pass, then click "
        f"the Sign In button. Finally, assert that the Dashboard page is shown."
    )

    planned_steps = await plan_test_case(description)
    assert len(planned_steps) >= 1

    async with BrowserSession(headless=True) as session:
        results = await run_executor(
            session.page,
            store,
            event_bus,
            run.run_id,
            "TC-INTEGRATION",
            planned_steps,
            log=lambda msg: store.add_log(run.run_id, msg),
        )

    assert all(r.status == "OK" for r in results), [r.model_dump() for r in results]

    execution_steps = store.get_execution_steps(run.run_id)
    assert len(execution_steps) > 0

    traces = store.get_agent_traces(run.run_id)
    assert any(t.message_type == "tool_call" for t in traces)
