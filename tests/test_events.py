"""Events and inbox: dedupe, ordering, Leader relevance, idempotent ack."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentctl.events import EVENT_NAMES, LEADER_RELEVANT, EventBus
from agentctl.storage import Store, open_connection
from agentctl.workspace.resolver import WorkspaceResolver


@pytest.fixture()
def env(tmp_path: Path):
    conn = open_connection(tmp_path / "registry.db")
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(tmp_path / "proj"))
    yield store, ws, EventBus(store)
    conn.close()


def test_event_is_persisted_with_schema_version(env) -> None:
    store, ws, bus = env
    emitted = bus.emit(ws, "session.created", source="test", payload={"session": "w1"})
    assert emitted.created is True
    stored = bus.list(ws)
    assert len(stored) == 1
    body = stored[0].to_dict()
    assert body["schema_version"] == 1
    assert body["event"] == "session.created"
    assert body["workspace_id"] == ws.id
    assert body["event_id"].startswith("evt_")


def test_unknown_event_name_is_refused(env) -> None:
    _store, ws, bus = env
    with pytest.raises(ValueError):
        bus.emit(ws, "sesssion.created")  # typo must not become history


def test_duplicate_dedupe_key_is_a_no_op(env) -> None:
    store, ws, bus = env
    first = bus.emit(ws, "turn.completed", session_id="sess_1", dedupe_key="sess_1:turn:3")
    second = bus.emit(ws, "turn.completed", session_id="sess_1", dedupe_key="sess_1:turn:3")
    assert first.created is True
    assert second.created is False
    assert second.event.id == first.event.id
    assert len(bus.list(ws)) == 1


def test_leader_relevant_events_enter_the_inbox(env) -> None:
    store, ws, bus = env
    bus.emit(ws, "turn.completed", session_id="sess_1", dedupe_key="a")
    bus.emit(ws, "session.created", session_id="sess_1", dedupe_key="b")
    pending = bus.pending_for_leader(ws)
    assert [item.event.event for item in pending] == ["turn.completed"]
    assert store.inbox.count_pending(ws.id) == 1


def test_inbox_ack_is_idempotent(env) -> None:
    store, ws, bus = env
    bus.emit(ws, "turn.completed", session_id="sess_1", dedupe_key="a")
    item = bus.pending_for_leader(ws)[0]
    assert store.inbox.ack(item.id, stamp="2026-01-01T00:00:00+00:00") is True
    assert store.inbox.ack(item.id, stamp="2026-01-01T00:00:01+00:00") is True
    assert store.inbox.count_pending(ws.id) == 0
    assert bus.pending_for_leader(ws) == []


def test_leader_relevant_is_a_subset_of_known_events() -> None:
    assert LEADER_RELEVANT <= EVENT_NAMES
    # turn.started must never wake a Leader: it is bookkeeping, not news.
    assert "turn.started" not in LEADER_RELEVANT


def test_severity_defaults_by_event_kind(env) -> None:
    _store, ws, bus = env
    bus.emit(ws, "message.failed", message_id="msg_1", dedupe_key="f")
    bus.emit(ws, "message.dispatched", message_id="msg_2", dedupe_key="d")
    rows = {event.event: event for event in bus.list(ws)}
    assert rows["message.failed"].severity == "error"
    assert rows["message.dispatched"].severity == "info"


def test_pagination_is_stable_across_same_timestamp(env) -> None:
    _store, ws, bus = env
    for index in range(5):
        bus.emit(ws, "session.status_changed", payload={"n": index})
    page = bus.list(ws, limit=2)
    assert [e.payload["n"] for e in page] == [0, 1]
    nxt = bus.list(ws, after_id=page[-1].id, limit=10)
    assert [e.payload["n"] for e in nxt] == [2, 3, 4]
