"""Spool ingest: exact ACK, workspace isolation, atomic claim, idempotence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agentctl.agents.detector as detector
from agentctl.config import default_config
from agentctl.errors import UsageError
from agentctl.events import EventBus
from agentctl.messaging.service import MessageService
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.lifecycle import LifecycleManager
from agentctl.session.service import CreateRequest, SessionService
from agentctl.spool import drain, spool_path
from agentctl.storage import Store, open_connection
from agentctl.workspace.resolver import WorkspaceResolver


@pytest.fixture()
def env(tmp_path: Path, workspace: Path, fake_hc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(detector, "installed", lambda definition: True)
    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(workspace))
    config = default_config()
    config.defaults.agent = "omp"
    service = SessionService(store, HerdrCoreAdapter(), config)
    session = service.create(ws, CreateRequest(name="w1", agent="omp", cwd=str(workspace))).session
    other = service.create(ws, CreateRequest(name="w2", agent="omp", cwd=str(workspace))).session
    messages = MessageService(
        store, HerdrCoreAdapter(), LifecycleManager(store, HerdrCoreAdapter()), bus=EventBus(store)
    )
    path = spool_path("omp")
    path.parent.mkdir(parents=True, exist_ok=True)
    yield store, ws, session, other, messages, path
    conn.close()


def _write(path: Path, *records: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_message_received_record_produces_the_exact_ack(env) -> None:
    store, ws, session, _other, messages, path = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    _write(
        path,
        {
            "kind": "message_received",
            "workspace_id": ws.id,
            "session_id": session.id,
            "agent": "omp",
            "message_id": message.id,
        },
    )
    report = drain(store, ws)
    assert report.rejected == []
    assert store.messages.find(message.id).state == "running"


def test_turn_completed_record_closes_the_message(env) -> None:
    store, ws, session, _other, messages, path = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    _write(
        path,
        {
            "kind": "turn_completed",
            "workspace_id": ws.id,
            "session_id": session.id,
            "agent": "omp",
            "message_id": message.id,
            "result": "all good",
        },
    )
    drain(store, ws)
    assert store.messages.find(message.id).state == "completed"
    completed = [e for e in EventBus(store).list(ws) if e.event == "turn.completed"]
    assert completed and completed[0].payload["result"] == "all good"


def test_replayed_records_change_nothing(env) -> None:
    store, ws, session, _other, messages, path = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    record = {
        "kind": "message_received",
        "workspace_id": ws.id,
        "session_id": session.id,
        "message_id": message.id,
    }
    for _ in range(3):
        _write(path, record)
        drain(store, ws)
    acks = [e for e in EventBus(store).list(ws) if e.event == "message.acknowledged"]
    assert len(acks) == 1


def test_another_workspaces_record_is_rejected(env) -> None:
    store, ws, session, _other, messages, path = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    _write(
        path,
        {
            "kind": "message_received",
            "workspace_id": "ws_somewhere_else",
            "session_id": session.id,
            "message_id": message.id,
        },
    )
    report = drain(store, ws)
    assert report.rejected and "workspace mismatch" in report.rejected[0]["reason"]
    assert store.messages.find(message.id).state == "dispatched"


def test_ack_from_the_wrong_session_is_rejected(env) -> None:
    store, ws, session, other, messages, path = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    _write(
        path,
        {
            "kind": "message_received",
            "workspace_id": ws.id,
            "session_id": other.id,
            "message_id": message.id,
        },
    )
    report = drain(store, ws)
    assert report.rejected
    assert store.messages.find(message.id).state == "dispatched"


def test_malformed_lines_are_reported_not_fatal(env) -> None:
    store, ws, session, _other, messages, path = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
        handle.write(json.dumps({"kind": "nonsense", "workspace_id": ws.id}) + "\n")
        handle.write(
            json.dumps(
                {
                    "kind": "message_received",
                    "workspace_id": ws.id,
                    "session_id": session.id,
                    "message_id": message.id,
                }
            )
            + "\n"
        )
    report = drain(store, ws)
    assert len(report.rejected) == 2
    assert len(report.applied) == 1
    assert store.messages.find(message.id).state == "running"


def test_drain_claims_the_file_atomically(env) -> None:
    store, ws, session, _other, _messages, path = env
    _write(path, {"kind": "session_started", "workspace_id": ws.id, "session_id": session.id})
    assert path.exists()
    first = drain(store, ws)
    assert first.records == 1
    assert not path.exists(), "the spool is claimed and removed by one drain"
    second = drain(store, ws)
    assert second.records == 0


def test_session_started_is_recorded_once(env) -> None:
    store, ws, session, _other, _messages, path = env
    for _ in range(2):
        _write(path, {"kind": "session_started", "workspace_id": ws.id, "session_id": session.id})
        drain(store, ws)
    ready = [e for e in EventBus(store).list(ws) if e.event == "session.ready"]
    assert len(ready) == 1


def test_turn_failed_marks_the_message_failed(env) -> None:
    store, ws, session, _other, messages, path = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    _write(
        path,
        {
            "kind": "turn_failed",
            "workspace_id": ws.id,
            "session_id": session.id,
            "message_id": message.id,
            "reason": "interrupted",
        },
    )
    drain(store, ws)
    assert store.messages.find(message.id).state == "failed"
    assert any(e.event == "turn.failed" for e in EventBus(store).list(ws))
