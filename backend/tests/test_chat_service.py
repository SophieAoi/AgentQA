from unittest.mock import patch

from agent.local_llm import LocalLLMError, ToolLoopTurn
from app.models.schemas import MessageRole
from app.services.chat_service import ChatService
from app.services.event_bus import EventBus
from app.services.store import InMemoryStore


def _fake_run_tool_loop(turns_to_yield):
    async def fake(**kwargs):
        for turn in turns_to_yield:
            yield turn

    return fake


def _text_turn(text: str):
    return ToolLoopTurn(type="text", text=text)


def _new_service(store, background_tasks=None):
    return ChatService(store, EventBus(), background_tasks or object())


async def test_reply_to_sends_full_history_without_duplicating_current_message():
    store = InMemoryStore()
    store.add_chat_message(MessageRole.user, "hi")
    store.add_chat_message(MessageRole.agent, "hello!")
    store.add_chat_message(MessageRole.user, "what can you do?")

    service = _new_service(store)
    fake = _fake_run_tool_loop([_text_turn("I can help with tests.")])
    with patch("app.services.chat_service.run_tool_loop", side_effect=fake) as mock_run_tool_loop:
        reply = await service.reply_to("what can you do?")

    assert reply == "I can help with tests."
    sent_messages = mock_run_tool_loop.call_args.kwargs["messages"]
    assert sent_messages == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello!"},
        {"role": "user", "content": "what can you do?"},
    ]
    # No duplicate trailing user turn
    assert sent_messages[-1] == {"role": "user", "content": "what can you do?"}


async def test_reply_to_caps_history_length():
    store = InMemoryStore()
    for i in range(30):
        store.add_chat_message(MessageRole.user, f"message {i}")

    service = _new_service(store)
    fake = _fake_run_tool_loop([_text_turn("ok")])
    with patch("app.services.chat_service.run_tool_loop", side_effect=fake) as mock_run_tool_loop:
        await service.reply_to("message 29")

    sent_messages = mock_run_tool_loop.call_args.kwargs["messages"]
    assert len(sent_messages) == 20


async def test_reply_to_handles_local_llm_connection_error_gracefully():
    store = InMemoryStore()
    service = _new_service(store)

    async def raising(**kwargs):
        raise LocalLLMError("could not reach the local model at http://localhost:11434")
        yield  # pragma: no cover — makes this an async generator function

    with patch("app.services.chat_service.run_tool_loop", side_effect=raising):
        reply = await service.reply_to("hi")

    assert "couldn't reach" in reply.lower()


async def test_reply_to_falls_back_when_no_text_turn_produced():
    store = InMemoryStore()
    service = _new_service(store)

    async def empty(**kwargs):
        return
        yield  # pragma: no cover

    with patch("app.services.chat_service.run_tool_loop", side_effect=empty):
        reply = await service.reply_to("hi")

    assert "try rephrasing" in reply.lower()
