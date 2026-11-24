"""agentctld: lease ownership, serial dispatch, crash recovery, honest backoff."""

from __future__ import annotations

from pathlib import Path

import pytest

import agentctl.agents.detector as detector
from agentctl.config import default_config
from agentctl.daemon.service import Daemon, DaemonConfig, Notifier, read_state, running_state
from agentctl.errors import AgentctlError
from agentctl.events import EventBus
from agentctl.leader.service import LeaderService
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.service import CreateRequest, SessionService
from agentctl.storage import Store, open_connection
from agentctl.storage.database import transaction
from agentctl.workspace.resolver import WorkspaceResolver


@pytest.fixture()
def env(tmp_path: Path, workspace: Path, fake_hc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(detector, "installed", lambda definition: True)
    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(workspace))
    config = default_config()
    config.defaults.agent = "omp"
    sessions = SessionService(store, HerdrCoreAdapter(), config)
    session = sessions.create(ws, CreateRequest(name="w1", agent="omp", cwd=str(workspace))).session
    daemon = Daemon(
        store,
        HerdrCoreAdapter(),
        ws,
        DaemonConfig(workspace_id=ws.id, instance_id="inst_daemon"),
    )
    yield store, ws, session, daemon
    conn.close()


def test_daemon_takes_the_lease_and_releases_it(env) -> None:
    store, ws, _session, daemon = env
    daemon.attach_leader()
    lease = LeaderService(store).current(ws)
    assert lease is not None and lease.held
    assert lease.leader_instance_id == "inst_daemon"
    daemon.release()
    assert LeaderService(store).current(ws).released_at is not None


def test_daemon_takes_over_an_expired_lease(env) -> None:
    store, ws, _session, daemon = env
    LeaderService(store).attach(ws, instance_id="someone_else")
    daemon.attach_leader()  # must not raise; it takes over
    assert LeaderService(store).current(ws).leader_instance_id == "inst_daemon"
    assert LeaderService(store).current(ws).leader_epoch == 2


def test_daemon_dispatches_a_queued_message(env) -> None:
    store, ws, session, daemon = env
    daemon.attach_leader()
    message = daemon.messages.enqueue(ws, session, "do the thing")
    daemon.pump_messages()
    assert store.messages.find(message.id).state == "dispatched"


def test_daemon_keeps_one_message_in_flight_per_worker(env) -> None:
    store, ws, session, daemon = env
    daemon.attach_leader()
    first = daemon.messages.enqueue(ws, session, "one")
    second = daemon.messages.enqueue(ws, session, "two")
    daemon.pump_messages()
    assert store.messages.find(first.id).state == "dispatched"
    assert store.messages.find(second.id).state == "queued"
    daemon.messages.complete(ws, first.id)
    daemon.pump_messages()
    assert store.messages.find(second.id).state == "dispatched"


def test_daemon_heals_a_stuck_dispatch(env) -> None:
    store, ws, session, daemon = env
    daemon.attach_leader()
    message = daemon.messages.enqueue(ws, session, "x")
    with transaction(store.conn):
        store.messages.update_state(
            message.id,
            "dispatching",
            expected=["queued"],
            set_dispatched=True,
            stamp="2000-01-01T00:00:00+00:00",
        )
    daemon.pump_messages()
    assert store.messages.find(message.id).state == "delivery_unknown"


def test_daemon_stops_dispatching_after_losing_the_lease(env) -> None:
    store, ws, session, daemon = env
    daemon.attach_leader()
    LeaderService(store).takeover(ws, instance_id="rival")
    daemon.heartbeat()  # discovers the loss
    waiting = daemon.messages.enqueue(ws, session, "x")
    daemon.pump_messages()
    assert store.messages.find(waiting.id).state == "queued", "a fenced Leader must not dispatch"


def test_notifier_reports_no_leader_when_the_lease_is_gone(env) -> None:
    store, ws, session, daemon = env
    message = daemon.messages.enqueue(ws, session, "x")
    bus = EventBus(store)
    bus.emit(ws, "turn.completed", session_id=session.id, message_id=message.id, dedupe_key="t1")
    item = bus.pending_for_leader(ws)[0]
    assert Notifier(store, bus).deliver(ws, item) == "no_leader"


def test_failed_notification_leaves_the_event_pending(env) -> None:
    store, ws, session, daemon = env
    daemon.attach_leader()
    message = daemon.messages.enqueue(ws, session, "x")
    daemon.bus.emit(ws, "turn.completed", session_id=session.id, message_id=message.id, dedupe_key="t1")
    daemon.pump_inbox()
    assert store.inbox.count_pending(ws.id) == 1, "an undeliverable event must survive"


def test_run_once_reports_pending_work(env) -> None:
    store, ws, session, daemon = env
    daemon.attach_leader()
    daemon.messages.enqueue(ws, session, "x")
    body = daemon.run_once()
    assert body["workspace_id"] == ws.id
    assert "pending_inbox" in body


def test_state_file_round_trip(env, tmp_path: Path, monkeypatch) -> None:
    store, ws, _session, daemon = env
    monkeypatch.setenv("AGENTCTL_HOME", str(tmp_path / "home2"))
    daemon.attach_leader()
    daemon.write_state()
    state = read_state()
    assert state is not None and state["workspace_id"] == ws.id
    assert state["pid"] == __import__("os").getpid()
    assert running_state() is not None
    daemon.release()
    assert read_state() is None
