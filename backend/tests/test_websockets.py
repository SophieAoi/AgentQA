"""
Phase 5 WebSocket tests: events published on the event bus reach a
connected client, in order, on the right channel — using FastAPI's
TestClient WS support rather than a real socket.
"""

from app.services.event_bus import get_event_bus


def test_logs_socket_closes_for_unknown_run(client):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/test-runs/does-not-exist/logs"):
            pass
    assert exc_info.value.code == 4004


def test_logs_socket_closes_for_unauthenticated_client(unauthenticated_client, store):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    run = store.create_test_run(["TC-001"])

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with unauthenticated_client.websocket_connect(f"/ws/test-runs/{run.run_id}/logs"):
            pass
    assert exc_info.value.code == 4401


def test_logs_socket_sends_hello_then_relays_published_events(client, store):
    run = store.create_test_run(["TC-001"])
    event_bus = get_event_bus()

    with client.websocket_connect(f"/ws/test-runs/{run.run_id}/logs") as ws:
        hello = ws.receive_json()
        assert hello == {"type": "connected", "run_id": run.run_id, "channel": "logs"}

        event_bus.publish(run.run_id, "logs", "log", {"message": "Starting TC-001..."})
        message = ws.receive_json()
        assert message == {"type": "log", "data": {"message": "Starting TC-001..."}}


def test_logs_socket_receives_events_in_order(client, store):
    run = store.create_test_run(["TC-001"])
    event_bus = get_event_bus()

    with client.websocket_connect(f"/ws/test-runs/{run.run_id}/logs") as ws:
        ws.receive_json()  # hello

        for i in range(3):
            event_bus.publish(run.run_id, "logs", "log", {"message": f"line {i}"})

        received = [ws.receive_json()["data"]["message"] for _ in range(3)]
        assert received == ["line 0", "line 1", "line 2"]


def test_browser_socket_only_receives_browser_channel_events(client, store):
    run = store.create_test_run(["TC-001"])
    event_bus = get_event_bus()

    with client.websocket_connect(f"/ws/test-runs/{run.run_id}/browser") as ws:
        ws.receive_json()  # hello

        # A "logs" publish must not leak onto the "browser" socket.
        event_bus.publish(run.run_id, "logs", "log", {"message": "should not arrive here"})
        event_bus.publish(run.run_id, "browser", "screenshot", {"image": "data:image/jpeg;base64,xyz"})

        message = ws.receive_json()
        assert message == {"type": "screenshot", "data": {"image": "data:image/jpeg;base64,xyz"}}


def test_event_bus_unsubscribes_on_disconnect(client, store):
    run = store.create_test_run(["TC-001"])
    event_bus = get_event_bus()

    with client.websocket_connect(f"/ws/test-runs/{run.run_id}/logs") as ws:
        ws.receive_json()  # hello
        assert len(event_bus._subscribers.get((run.run_id, "logs"), [])) == 1

    assert (run.run_id, "logs") not in event_bus._subscribers
