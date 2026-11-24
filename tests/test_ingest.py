"""Herdr Core fact changes become agentctl events exactly once."""

from __future__ import annotations

from pathlib import Path

import pytest

import agentctl.agents.detector as detector
from agentctl.config import default_config
from agentctl.events import EventBus
from agentctl.events.ingest import Ingestor, TerminalView
from agentctl.runtime.herdr_core import HerdrCoreAdapter, event_command, parse_event_line
from agentctl.session.service import CreateRequest, SessionService
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
    sessions = SessionService(store, HerdrCoreAdapter(), config)
    session = sessions.create(ws, CreateRequest(name="w1", agent="omp", cwd=str(workspace))).session
    bus = EventBus(store)
    yield store, ws, session, bus, Ingestor(store, bus)
    conn.close()


def _view(session, **overrides) -> TerminalView:
    base = TerminalView(
        address=session.hc_terminal_id or "t1",
        labels={"agentctl.workspace": session.workspace_id, "agentctl.session": session.id},
        reported=True,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_status_done_emits_one_turn_completed(env) -> None:
    store, ws, session, bus, ingestor = env
    ingestor.observe(ws, _view(session, status="working", turn=1))
    ingestor.observe(ws, _view(session, status="done", turn=1))
    ingestor.observe(ws, _view(session, status="done", turn=1))  # replayed pair
    completions = [e for e in bus.list(ws) if e.event == "turn.completed"]
    assert len(completions) == 1


def test_turn_event_after_status_done_does_not_duplicate(env) -> None:
    store, ws, session, bus, ingestor = env
    # hc reports `status: done` and then a separate `turn` bump, in that order.
    ingestor.observe(ws, _view(session, status="done", turn=2))
    ingestor.observe(ws, _view(session, status="done", turn=2))
    assert len([e for e in bus.list(ws) if e.event == "turn.completed"]) == 1


def test_next_turn_is_a_new_event(env) -> None:
    store, ws, session, bus, ingestor = env
    ingestor.observe(ws, _view(session, status="done", turn=1))
    ingestor.observe(ws, _view(session, status="done", turn=2))
    assert len([e for e in bus.list(ws) if e.event == "turn.completed"]) == 2


def test_blocked_is_reported_once_per_stretch(env) -> None:
    store, ws, session, bus, ingestor = env
    ingestor.observe(ws, _view(session, status="blocked", turn=1))
    ingestor.observe(ws, _view(session, status="blocked", turn=1))
    ingestor.observe(ws, _view(session, status="working", turn=1))
    ingestor.observe(ws, _view(session, status="blocked", turn=1))
    assert len([e for e in bus.list(ws) if e.event == "worker.blocked"]) == 2


def test_finished_is_reported_once(env) -> None:
    store, ws, session, bus, ingestor = env
    ingestor.observe(ws, _view(session, state="finished", reason="exited", exit_code=0))
    ingestor.observe(ws, _view(session, state="finished", reason="exited", exit_code=0))
    exits = [e for e in bus.list(ws) if e.event == "session.exited"]
    assert len(exits) == 1
    assert exits[0].payload["exit_code"] == 0


def test_another_workspaces_terminal_is_never_adopted(env) -> None:
    store, ws, session, bus, ingestor = env
    foreign = TerminalView(
        address="t9",
        status="done",
        reported=True,
        labels={"agentctl.workspace": "ws_other", "agentctl.session": session.id},
    )
    ingestor.observe(ws, foreign)
    assert bus.list(ws) == []


def test_terminal_without_labels_is_matched_by_persisted_address(env) -> None:
    store, ws, session, bus, ingestor = env
    ingestor.observe(ws, TerminalView(address=session.hc_terminal_id, status="done", reported=True))
    assert [e.event for e in bus.list(ws)] == ["turn.completed"]


def test_unlabelled_unknown_terminal_is_ignored(env) -> None:
    store, ws, session, bus, ingestor = env
    ingestor.observe(ws, TerminalView(address="t99", status="done", reported=True))
    assert bus.list(ws) == []


def test_parse_event_line_classifies_measured_formats() -> None:
    snapshot = parse_event_line('{"channel":null,"seq":4,"terminals":[]}')
    assert snapshot and snapshot[0] == "snapshot"
    event = parse_event_line(
        '{"jsonrpc":"2.0","method":"event","params":{"seq":8,"terminal":"t2","type":"status",'
        '"data":{"reported":true,"status":{"app":"omp","state":"idle"}}}}'
    )
    assert event and event[0] == "event"
    assert event[1]["type"] == "status"
    heartbeat = parse_event_line('{"seq":9}')
    assert heartbeat and heartbeat[0] == "heartbeat"
    assert parse_event_line("") is None
    assert parse_event_line("not json") is None


def test_event_command_filters_by_workspace_label() -> None:
    assert event_command("hc", labels={"agentctl.workspace": "ws_1"}) == [
        "hc",
        "events",
        "--json",
        "--label",
        "agentctl.workspace=ws_1",
    ]
