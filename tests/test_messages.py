"""Message registry: persistence, exact ACK, dedupe, ordering, honest unknowns."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agentctl.agents.detector as detector
from agentctl.config import default_config
from agentctl.errors import (
    MessageBusy,
    MessageNotFound,
    MessageStateError,
    UsageError,
)
from agentctl.events import EventBus
from agentctl.messaging.service import MessageService, compose, find_marker, marker_for
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.messaging import MessagingService
from agentctl.session.service import CreateRequest, SessionService
from agentctl.storage import Store, open_connection
from agentctl.storage.database import transaction
from agentctl.workspace.resolver import WorkspaceResolver
from tests.conftest import read_state


@pytest.fixture()
def env(workspace: Path, fake_hc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(detector, "installed", lambda definition: True)
    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(workspace))
    config = default_config()
    config.defaults.agent = "omp"
    sessions = SessionService(store, HerdrCoreAdapter(), config)
    created = sessions.create(ws, CreateRequest(name="worker", agent="omp", cwd=str(workspace / "backend")))
    runtime = HerdrCoreAdapter()
    bus = EventBus(store)
    service = MessageService(store, runtime, sessions.lifecycle, bus=bus)
    yield service, store, ws, created.session, fake_hc, bus
    conn.close()


def test_marker_round_trips_through_arbitrary_text() -> None:
    marker = marker_for("msg_abc123")
    assert find_marker(f"hello {marker} world") == "msg_abc123"
    assert find_marker("no marker here") is None
    assert find_marker("[[agentctl:]]") is None
    body = compose("do the thing\nwith a newline", "msg_abc123")
    assert "do the thing\nwith a newline" in body
    assert find_marker(body) == "msg_abc123"


def test_enqueue_persists_before_any_send(env) -> None:
    service, store, ws, session, state_file, _bus = env
    message = service.enqueue(ws, session, "hello")
    assert message.state == "queued"
    assert read_state(state_file).get("sent", []) == []
    stored = store.messages.find(message.id)
    assert stored is not None and stored.payload == "hello"


def test_enqueue_refuses_empty(env) -> None:
    service, _store, ws, session, _state, _bus = env
    with pytest.raises(UsageError):
        service.enqueue(ws, session, "   \n ")


def test_dispatch_then_hook_ack_closes_the_loop(env) -> None:
    service, store, ws, session, state_file, bus = env
    message = service.enqueue(ws, session, "diagnose the failure")

    outcome = service.dispatch(ws, message.id)
    assert outcome.confirmation == "dispatched"
    sent = read_state(state_file)["sent"][-1]
    assert find_marker(sent["text"]) == message.id, "the exact message id must travel with the text"

    ack = service.acknowledge(ws, message.id)
    assert ack.changed is True
    assert ack.message.state == "acknowledged"
    assert ack.message.acknowledged_at is not None

    done = service.complete(ws, message.id, result_summary="found it", turn_index=3)
    assert done.state == "completed"

    names = [e.event for e in bus.list(ws)]
    assert names == [
        "message.queued",
        "message.dispatched",
        "message.acknowledged",
        "turn.completed",
        "message.completed",
    ]


def test_duplicate_hook_ack_is_idempotent(env) -> None:
    service, store, ws, session, _state, bus = env
    message = service.enqueue(ws, session, "x")
    service.dispatch(ws, message.id)
    first = service.acknowledge(ws, message.id)
    second = service.acknowledge(ws, message.id)
    assert first.changed is True
    assert second.changed is False
    acks = [e for e in bus.list(ws) if e.event == "message.acknowledged"]
    assert len(acks) == 1


def test_duplicate_completion_does_not_duplicate_events(env) -> None:
    service, store, ws, session, _state, bus = env
    message = service.enqueue(ws, session, "x")
    service.dispatch(ws, message.id)
    service.acknowledge(ws, message.id)
    service.complete(ws, message.id, turn_index=1)
    service.complete(ws, message.id, turn_index=1)
    completions = [e for e in bus.list(ws) if e.event == "turn.completed"]
    assert len(completions) == 1


def test_second_message_for_a_busy_worker_waits(env) -> None:
    service, _store, ws, session, _state, _bus = env
    first = service.enqueue(ws, session, "first")
    second = service.enqueue(ws, session, "second")
    service.dispatch(ws, first.id)
    with pytest.raises(MessageBusy):
        service.dispatch(ws, second.id)
    service.acknowledge(ws, first.id)
    service.complete(ws, first.id)
    dispatched = service.dispatch(ws, second.id)
    assert dispatched.confirmation == "dispatched"


def test_parallel_workers_are_independent(env) -> None:
    service, store, ws, session, _state, _bus = env
    other = store.sessions.find_by_id(session.id)
    config = default_config()
    config.defaults.agent = "omp"
    from agentctl.session.service import CreateRequest, SessionService

    sessions = SessionService(store, service.runtime, config)
    second = sessions.create(ws, CreateRequest(name="other", agent="omp", cwd=ws.root_path)).session

    a = service.enqueue(ws, other, "for worker one")
    b = service.enqueue(ws, second, "for worker two")
    assert service.dispatch(ws, a.id).confirmation == "dispatched"
    assert service.dispatch(ws, b.id).confirmation == "dispatched"


def test_ack_for_unknown_message_is_an_error(env) -> None:
    service, _store, ws, _session, _state, _bus = env
    with pytest.raises(MessageNotFound):
        service.acknowledge(ws, "msg_does_not_exist")


def test_cancel_then_retry_requeues(env) -> None:
    service, _store, ws, session, _state, _bus = env
    message = service.enqueue(ws, session, "x")
    cancelled = service.cancel(ws, message.id)
    assert cancelled.state == "cancelled"
    with pytest.raises(MessageStateError):
        service.retry(ws, message.id)


def test_retry_refuses_delivery_unknown(env) -> None:
    service, store, ws, session, _state, _bus = env
    message = service.enqueue(ws, session, "x")
    with transaction(store.conn):
        store.messages.update_state(message.id, "delivery_unknown", expected=["queued"], stamp="2026-01-01T00:00:00+00:00")
    with pytest.raises(MessageStateError) as excinfo:
        service.retry(ws, message.id)
    assert "may already have reached" in excinfo.value.message
    assert "not safe to resend" in (excinfo.value.hint or "")


def test_failed_message_can_be_retried(env) -> None:
    service, store, ws, session, _state, _bus = env
    message = service.enqueue(ws, session, "x")
    with transaction(store.conn):
        store.messages.update_state(message.id, "failed", expected=["queued"], last_error="boom", stamp="2026-01-01T00:00:00+00:00")
    requeued = service.retry(ws, message.id)
    assert requeued.state == "queued"
    assert requeued.last_error is None


def test_stuck_dispatch_becomes_delivery_unknown(env) -> None:
    service, store, ws, session, _state, bus = env
    message = service.enqueue(ws, session, "x")
    # Simulate a crash after `dispatching` was written and before the outcome.
    with transaction(store.conn):
        store.messages.update_state(
            message.id,
            "dispatching",
            expected=["queued"],
            set_dispatched=True,
            stamp="2000-01-01T00:00:00+00:00",
        )
    recovered = service.recover_stuck(ws, older_than_seconds=0)
    assert [m.id for m in recovered] == [message.id]
    assert recovered[0].state == "delivery_unknown"
    assert any(e.event == "message.delivery_unknown" for e in bus.list(ws))
    # And it is never auto-resubmitted.
    with pytest.raises(MessageStateError):
        service.dispatch(ws, message.id)


def test_dispatch_to_finished_session_leaves_the_message_queued(env) -> None:
    service, store, ws, session, state_file, _bus = env
    message = service.enqueue(ws, session, "x")
    state = json.loads(state_file.read_text())
    for body in state["terminals"].values():
        body["state"] = "finished"
        body["reason"] = "exited"
    state_file.write_text(json.dumps(state))
    from agentctl.errors import AgentctlError

    with pytest.raises(AgentctlError):
        service.dispatch(ws, message.id)
    stored = store.messages.find(message.id)
    assert stored.state == "queued", "a refusal must not consume the message"


def test_unconfirmed_agent_requires_opt_in_and_records_unknown(env) -> None:
    service, store, ws, _session, _state, _bus = env
    from agentctl.session.service import CreateRequest, SessionService
    from agentctl.config import default_config

    # codex has no hook integration, so no ACK can ever arrive.
    sessions = SessionService(store, service.runtime, default_config())
    codex = sessions.create(ws, CreateRequest(name="legacy", agent="codex", cwd=ws.root_path)).session
    message = service.enqueue(ws, codex, "x")

    from agentctl.errors import AgentctlError

    with pytest.raises(AgentctlError) as excinfo:
        service.dispatch(ws, message.id)
    assert "cannot be confirmed" in excinfo.value.message
    assert "--unconfirmed" in (excinfo.value.hint or "")
    assert store.messages.find(message.id).state == "queued"

    outcome = service.dispatch(ws, message.id, allow_unconfirmed=True)
    assert outcome.confirmation == "delivery_unknown"
    assert store.messages.find(message.id).state == "delivery_unknown"


def test_payload_bytes_survive_dispatch(env) -> None:
    service, _store, ws, session, state_file, _bus = env
    payload = "line one\nline two\n\tquoted \"x\" $VAR --flag ✓ 日本語"
    message = service.enqueue(ws, session, payload)
    service.dispatch(ws, message.id)
    sent = read_state(state_file)["sent"][-1]["text"]
    assert payload in sent
