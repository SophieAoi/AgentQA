import time

from app.models.schemas import ChatMessage, MessageRole
from app.services.store import InMemoryStore


def test_store_conforms_to_protocol_and_isolates_state():
    store = InMemoryStore()
    store.add_chat_message(MessageRole.user, "hello")
    assert len(store.get_chat_history()) == 1

    other_store = InMemoryStore()
    assert other_store.get_chat_history() == []


def test_create_and_get_test_run_round_trips():
    store = InMemoryStore()
    run = store.create_test_run(["TC-001"])
    fetched = store.get_test_run(run.run_id)
    assert fetched is not None
    assert fetched.run_id == run.run_id
    assert fetched.total_count == 1


def test_list_test_runs_returns_all_created_runs():
    store = InMemoryStore()
    store.create_test_run(["TC-001"])
    store.create_test_run(["TC-002"])
    assert len(store.list_test_runs()) == 2


def test_update_test_run_is_noop_for_unknown_run_id():
    store = InMemoryStore()
    store.update_test_run("does-not-exist", status="passed")  # should not raise


def test_add_log_is_noop_for_unknown_run_id():
    store = InMemoryStore()
    store.add_log("does-not-exist", "hello")  # should not raise


def test_chat_message_default_timestamp_is_not_frozen_at_import_time():
    """
    Regression test for audit finding F-001: ChatMessage.timestamp used to
    default to datetime.utcnow() evaluated once at class-definition time,
    so two instances built without an explicit timestamp got an identical,
    stale value. Field(default_factory=...) fixes this.
    """
    first = ChatMessage(role=MessageRole.user, content="a")
    time.sleep(0.01)
    second = ChatMessage(role=MessageRole.user, content="b")
    assert first.timestamp != second.timestamp
