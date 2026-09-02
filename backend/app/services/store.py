"""
Store seam for chat history, test runs, and (from Phase 3) agent execution
detail — the structured per-step record and reasoning trace the Planner/
Executor loop produces, in addition to the frontend-facing TestRunDetail
summary.

StoreProtocol defines the interface every router/service depends on via
FastAPI's Depends(get_store) instead of importing this module directly.
InMemoryStore is the current dict-backed implementation — still in-memory,
resets on restart — but swapping in a MongoDB-backed implementation later
only means writing a new class that satisfies StoreProtocol, not touching
any router or service that consumes it.
"""

import uuid
from datetime import datetime
from typing import Optional, Protocol

from app.models.schemas import (
    AgentTrace,
    ChatMessage,
    ExecutionStep,
    MessageRole,
    TestRunDetail,
    TestRunStatus,
)


class StoreProtocol(Protocol):
    def add_chat_message(self, role: MessageRole, content: str) -> ChatMessage: ...

    def get_chat_history(self) -> list[ChatMessage]: ...

    def create_test_run(self, test_case_ids: list[str]) -> TestRunDetail: ...

    def get_test_run(self, run_id: str) -> Optional[TestRunDetail]: ...

    def list_test_runs(self) -> list[TestRunDetail]: ...

    def update_test_run(self, run_id: str, **kwargs) -> None: ...

    def add_log(self, run_id: str, message: str) -> None: ...

    def add_execution_step(self, step: ExecutionStep) -> None: ...

    def get_execution_steps(self, run_id: str) -> list[ExecutionStep]: ...

    def add_agent_trace(self, trace: AgentTrace) -> None: ...

    def get_agent_traces(self, run_id: str) -> list[AgentTrace]: ...


class InMemoryStore:
    """Dict-backed StoreProtocol implementation. Resets every server restart."""

    def __init__(self) -> None:
        self._chat_history: list[ChatMessage] = []
        self._test_runs: dict[str, TestRunDetail] = {}
        self._execution_steps: dict[str, list[ExecutionStep]] = {}
        self._agent_traces: dict[str, list[AgentTrace]] = {}

    def add_chat_message(self, role: MessageRole, content: str) -> ChatMessage:
        msg = ChatMessage(role=role, content=content, timestamp=datetime.utcnow())
        self._chat_history.append(msg)
        return msg

    def get_chat_history(self) -> list[ChatMessage]:
        return self._chat_history

    def create_test_run(self, test_case_ids: list[str]) -> TestRunDetail:
        run_id = str(uuid.uuid4())[:8]
        run = TestRunDetail(
            run_id=run_id,
            status=TestRunStatus.queued,
            started_at=datetime.utcnow(),
            total_count=len(test_case_ids),
        )
        self._test_runs[run_id] = run
        return run

    def get_test_run(self, run_id: str) -> Optional[TestRunDetail]:
        return self._test_runs.get(run_id)

    def list_test_runs(self) -> list[TestRunDetail]:
        return list(self._test_runs.values())

    def update_test_run(self, run_id: str, **kwargs) -> None:
        run = self._test_runs.get(run_id)
        if not run:
            return
        for key, value in kwargs.items():
            setattr(run, key, value)

    def add_log(self, run_id: str, message: str) -> None:
        run = self._test_runs.get(run_id)
        if not run:
            return
        timestamp = datetime.utcnow().strftime("%H:%M:%S")
        run.logs.append(f"{timestamp}  {message}")

    def add_execution_step(self, step: ExecutionStep) -> None:
        self._execution_steps.setdefault(step.run_id, []).append(step)

    def get_execution_steps(self, run_id: str) -> list[ExecutionStep]:
        return self._execution_steps.get(run_id, [])

    def add_agent_trace(self, trace: AgentTrace) -> None:
        self._agent_traces.setdefault(trace.run_id, []).append(trace)

    def get_agent_traces(self, run_id: str) -> list[AgentTrace]:
        return self._agent_traces.get(run_id, [])


_store_instance = InMemoryStore()


def get_store() -> StoreProtocol:
    return _store_instance
