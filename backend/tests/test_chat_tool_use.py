"""
Phase 6 chat tool-use tests: a natural-language "run TC-001" message
actually starts a real run (mocked local-model deciding to call the tool,
real store interaction and a real background task registration).
"""

from unittest.mock import MagicMock, patch

from agent.local_llm import ToolLoopTurn
from agent.runner import list_test_cases
from app.models.schemas import MessageRole
from app.services.chat_service import ChatService
from app.services.event_bus import EventBus
from app.services.store import InMemoryStore


def _tool_calling_fake(tool_name: str, tool_input: dict):
    """
    Actually invokes the real tool function matching tool_name — needed to
    exercise start_test_run's real side effects (store writes, background
    task registration), not just its text framing.
    """

    async def fake(**kwargs):
        tools_by_name = {t.name: t for t in kwargs.get("tools", [])}
        tool = tools_by_name[tool_name]
        result = await tool(**tool_input)
        yield ToolLoopTurn(type="tool_call", tool_name=tool_name, tool_input=tool_input, tool_result=result)
        yield ToolLoopTurn(type="text", text=f"Sure — {result}")

    return fake


def _new_service(store, event_bus, background_tasks):
    return ChatService(store, event_bus, background_tasks)


async def test_chat_message_triggers_a_real_test_run():
    store = InMemoryStore()
    store.add_chat_message(MessageRole.user, "run TC-001")
    event_bus = EventBus()
    background_tasks = MagicMock()

    service = _new_service(store, event_bus, background_tasks)
    fake = _tool_calling_fake("start_test_run", {"test_case_ids": ["TC-001"]})

    with patch("app.services.chat_service.run_tool_loop", side_effect=fake):
        reply = await service.reply_to("run TC-001")

    # Real store interaction: a run was actually created.
    runs = store.list_test_runs()
    assert len(runs) == 1
    run_id = runs[0].run_id
    assert run_id in reply

    # The background task was scheduled with the real run_test_suite coroutine fn.
    background_tasks.add_task.assert_called_once()
    call_args = background_tasks.add_task.call_args.args
    assert call_args[0].__name__ == "run_test_suite"
    assert call_args[1] is store
    assert call_args[2] is event_bus
    assert call_args[3] == run_id
    assert call_args[4] == ["TC-001"]


async def test_chat_message_rejects_unknown_test_case_id():
    store = InMemoryStore()
    event_bus = EventBus()
    background_tasks = MagicMock()
    service = _new_service(store, event_bus, background_tasks)
    fake = _tool_calling_fake("start_test_run", {"test_case_ids": ["TC-999"]})

    with patch("app.services.chat_service.run_tool_loop", side_effect=fake):
        reply = await service.reply_to("run TC-999")

    assert store.list_test_runs() == []
    background_tasks.add_task.assert_not_called()
    assert "TC-999" in reply


async def test_chat_message_run_all_looks_up_ids_before_starting_a_run():
    """
    Regression test: "run all of them" previously made the model ask the
    user to enumerate exact IDs, since it had no way to look them up. It
    should now call list_test_cases first, then start_test_run with
    everything that came back.
    """
    store = InMemoryStore()
    event_bus = EventBus()
    background_tasks = MagicMock()
    service = _new_service(store, event_bus, background_tasks)

    all_ids = [case["id"] for case in list_test_cases()]

    async def fake(**kwargs):
        tools_by_name = {t.name: t for t in kwargs.get("tools", [])}
        listing = await tools_by_name["list_test_cases"]()
        assert all_ids[0] in listing  # the tool actually returned real data
        result = await tools_by_name["start_test_run"](test_case_ids=all_ids)
        yield ToolLoopTurn(type="tool_call", tool_name="list_test_cases", tool_input={}, tool_result=listing)
        yield ToolLoopTurn(
            type="tool_call", tool_name="start_test_run", tool_input={"test_case_ids": all_ids},
            tool_result=result,
        )
        yield ToolLoopTurn(type="text", text=f"Sure — {result}")

    with patch("app.services.chat_service.run_tool_loop", side_effect=fake):
        reply = await service.reply_to("run all of them")

    runs = store.list_test_runs()
    assert len(runs) == 1
    assert runs[0].total_count == len(all_ids)
    assert runs[0].run_id in reply


async def test_chat_message_without_a_run_request_never_calls_the_tool():
    """A normal conversational reply shouldn't touch the store at all."""
    store = InMemoryStore()
    event_bus = EventBus()
    background_tasks = MagicMock()
    service = _new_service(store, event_bus, background_tasks)

    async def plain_text(**kwargs):
        yield ToolLoopTurn(
            type="text", text="TC-001 checks that a Guaranteed deal can be created with valid inputs."
        )

    with patch("app.services.chat_service.run_tool_loop", side_effect=plain_text):
        reply = await service.reply_to("what does TC-001 check?")

    assert store.list_test_runs() == []
    background_tasks.add_task.assert_not_called()
    assert "Guaranteed deal" in reply
