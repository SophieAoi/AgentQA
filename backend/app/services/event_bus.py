"""
In-process pub/sub for live test-run events (log lines, step results,
screenshot frames). One asyncio.Queue per subscriber, keyed by run_id —
agent_runner.py/agent/runner.py/agent/executor.py publish as they already
write to the store (in addition to, not instead of — the store stays the
source of truth for GET /test-runs/{id} and reconnect reconciliation); the
WebSocket endpoints in app/routers/websockets.py subscribe and forward.

Single-process only, same scope as store.py's InMemoryStore — the
documented upgrade path is Redis pub/sub if/when multi-worker deployment
lands (phase 7+ backlog), not built now.
"""

import asyncio
from dataclasses import dataclass
from typing import Literal


@dataclass
class RunEvent:
    run_id: str
    channel: Literal["logs", "browser"]
    type: str
    data: dict


class EventBus:
    def __init__(self) -> None:
        # Keyed by (run_id, channel) — a "logs" subscriber's queue never
        # receives "browser" screenshot frames and vice versa, so a
        # log-only client isn't paying for large frame payloads it discards.
        self._subscribers: dict[tuple[str, str], list[asyncio.Queue]] = {}

    def subscribe(self, run_id: str, channel: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault((run_id, channel), []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, channel: str, queue: asyncio.Queue) -> None:
        key = (run_id, channel)
        subs = self._subscribers.get(key)
        if not subs or queue not in subs:
            return
        subs.remove(queue)
        if not subs:
            self._subscribers.pop(key, None)

    def publish(self, run_id: str, channel: str, event_type: str, data: dict) -> None:
        subs = self._subscribers.get((run_id, channel))
        if not subs:
            return  # no connected clients — cheap no-op, never blocks the agent loop
        event = RunEvent(run_id=run_id, channel=channel, type=event_type, data=data)
        for queue in subs:
            queue.put_nowait(event)


_bus_instance = EventBus()


def get_event_bus() -> EventBus:
    return _bus_instance
