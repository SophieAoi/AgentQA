"""
Live streaming endpoints (phase 5, docs/phase-05-live-streaming-websockets.md).
Purely a new transport layer over data that already exists — the store stays
the source of truth (GET /test-runs/{id} still works with zero clients ever
connecting here); these sockets just forward event_bus publications so a
connected client doesn't have to poll.

WS /ws/test-runs/{run_id}/logs    — log lines, step results, agent traces.
WS /ws/test-runs/{run_id}/browser — periodic screenshot frames.
"""

import asyncio

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.services.auth_service import AuthStoreProtocol, get_auth_store
from app.services.event_bus import EventBus, get_event_bus
from app.services.store import StoreProtocol, get_store

router = APIRouter(prefix="/ws/test-runs", tags=["websockets"])


async def _authenticate(websocket: WebSocket, auth_store: AuthStoreProtocol) -> bool:
    """
    Manual cookie check rather than Depends(get_current_user): raising
    HTTPException from a dependency doesn't reliably close a WebSocket
    handshake cleanly across FastAPI/Starlette versions, so this mirrors the
    existing manual-close pattern already used here for the unknown-run-id
    case (close code 4004) — 4401 here for "unauthenticated."
    """
    token = websocket.cookies.get("session")
    if not token:
        await websocket.close(code=4401, reason="Not authenticated")
        return False
    session = auth_store.get_session(token)
    if not session or not auth_store.get_user_by_id(session.user_id):
        await websocket.close(code=4401, reason="Not authenticated")
        return False
    return True


async def _stream_channel(websocket: WebSocket, run_id: str, channel: str, event_bus: EventBus) -> None:
    await websocket.accept()
    queue = event_bus.subscribe(run_id, channel)
    try:
        # A client that connects after the run already reached a terminal
        # status would otherwise hang waiting for events that will never
        # come — send one hello so the frontend can distinguish "connected,
        # waiting" from a dead socket, then just relay published events.
        await websocket.send_json({"type": "connected", "run_id": run_id, "channel": channel})

        # recv_task is created once and reused across iterations — only
        # get_task is one-shot (queue.get() must be reissued each time).
        # Recreating recv_task on every loop turn would leak the previous
        # still-pending receive_text() call and risk Starlette's "concurrent
        # call to receive()" error.
        recv_task = asyncio.ensure_future(websocket.receive_text())
        try:
            while True:
                get_task = asyncio.ensure_future(queue.get())
                try:
                    done, _pending = await asyncio.wait(
                        {recv_task, get_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if get_task in done:
                        event = get_task.result()
                        await websocket.send_json({"type": event.type, "data": event.data})
                    else:
                        get_task.cancel()

                    if recv_task in done:
                        # Clients don't send anything meaningful — this only
                        # exists to detect a client-initiated close promptly.
                        break
                except Exception:
                    get_task.cancel()
                    raise
        finally:
            recv_task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(run_id, channel, queue)


@router.websocket("/{run_id}/logs")
async def stream_logs(
    websocket: WebSocket,
    run_id: str,
    event_bus: EventBus = Depends(get_event_bus),
    store: StoreProtocol = Depends(get_store),
    auth_store: AuthStoreProtocol = Depends(get_auth_store),
):
    if not await _authenticate(websocket, auth_store):
        return
    if not store.get_test_run(run_id):
        await websocket.close(code=4004, reason="Test run not found")
        return
    await _stream_channel(websocket, run_id, "logs", event_bus)


@router.websocket("/{run_id}/browser")
async def stream_browser(
    websocket: WebSocket,
    run_id: str,
    event_bus: EventBus = Depends(get_event_bus),
    store: StoreProtocol = Depends(get_store),
    auth_store: AuthStoreProtocol = Depends(get_auth_store),
):
    if not await _authenticate(websocket, auth_store):
        return
    if not store.get_test_run(run_id):
        await websocket.close(code=4004, reason="Test run not found")
        return
    await _stream_channel(websocket, run_id, "browser", event_bus)
