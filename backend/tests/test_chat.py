from unittest.mock import patch

from agent.local_llm import ToolLoopTurn


def test_send_message_then_appears_in_history(client):
    async def fake_run_tool_loop(**kwargs):
        yield ToolLoopTurn(type="text", text="hello there")

    with patch("app.services.chat_service.run_tool_loop", side_effect=fake_run_tool_loop):
        response = client.post("/chat/message", json={"message": "hi"})
    assert response.status_code == 200
    assert response.json() == {"reply": "hello there"}

    history = client.get("/chat/history")
    assert history.status_code == 200
    roles = [msg["role"] for msg in history.json()]
    assert roles == ["user", "agent"]


def test_history_is_empty_for_a_fresh_store(client):
    response = client.get("/chat/history")
    assert response.status_code == 200
    assert response.json() == []
