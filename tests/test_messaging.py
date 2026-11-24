"""Messaging: delivery proof, key presses and interrupts."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import agentctl.agents.detector as detector
from agentctl.config import default_config
from agentctl.errors import DeliveryUnconfirmed, UsageError
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.messaging import MessagingService
from agentctl.session.service import CreateRequest, SessionService
from agentctl.storage import Store, open_connection
from agentctl.workspace.resolver import WorkspaceResolver
from tests.conftest import calls, read_state


@pytest.fixture()
def setup(workspace: Path, fake_hc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(detector, "installed", lambda definition: True)
    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(workspace))
    config = default_config()
    config.defaults.agent = "omp"
    service = SessionService(store, HerdrCoreAdapter(), config)
    result = service.create(ws, CreateRequest(name="worker", agent="omp", cwd=str(workspace / "backend")))
    messaging = MessagingService(service.runtime, service.lifecycle)
    yield messaging, service, store, ws, fake_hc, result.session
    conn.close()


def _set_reported(state_file: Path, address: str, reported: bool) -> None:
    state = json.loads(state_file.read_text())
    for body in state["terminals"].values():
        if body["address"] == address:
            body["reported"] = reported
    state_file.write_text(json.dumps(state))


def test_send_confirms_when_agent_reports(setup) -> None:
    messaging, _service, _store, _ws, state_file, session = setup
    report = messaging.send(session, "hello")
    assert report.confirmed is True
    assert len(read_state(state_file)["sent"]) == 1


def test_send_refuses_unreported_agent(setup) -> None:
    messaging, _service, _store, _ws, state_file, session = setup
    address = session.hc_terminal_id
    assert address is not None
    _set_reported(state_file, address, False)
    with pytest.raises(DeliveryUnconfirmed):
        messaging.send(session, "hello", ready_timeout=0.05)
    assert read_state(state_file).get("sent", []) == []


def test_send_force_bypasses_report(setup) -> None:
    messaging, _service, _store, _ws, state_file, session = setup
    address = session.hc_terminal_id
    assert address is not None
    _set_reported(state_file, address, False)
    report = messaging.send(session, "hello", force=True)
    assert report.confirmed is False
    assert len(read_state(state_file)["sent"]) == 1
    assert "--force" in calls(state_file)[-1]


def test_send_fails_fast_for_non_reporting_agent(
    setup, workspace: Path
) -> None:
    messaging, service, _store, ws, state_file, _session = setup
    session = service.create(
        ws, CreateRequest(name="codex-worker", agent="codex", cwd=str(workspace))
    ).session
    started = time.perf_counter()
    with pytest.raises(DeliveryUnconfirmed):
        messaging.send(session, "hello", ready_timeout=30)
    assert time.perf_counter() - started < 1.0
    assert read_state(state_file).get("sent", []) == []


def test_keys_passes_through(setup) -> None:
    messaging, _service, _store, _ws, state_file, session = setup
    report = messaging.keys(session, ["down", "enter"])
    assert report.turn == 1
    assert calls(state_file)[-1] == [
        "keys",
        "--json",
        session.hc_terminal_id,
        "down",
        "enter",
    ]


def test_keys_rejects_option_like_names(setup) -> None:
    messaging, _service, _store, _ws, _state_file, session = setup
    with pytest.raises(UsageError):
        messaging.keys(session, ["--force"])
    with pytest.raises(UsageError):
        messaging.keys(session, [])


def test_interrupt_passes_through(setup) -> None:
    messaging, _service, _store, _ws, state_file, session = setup
    report = messaging.interrupt(session, timeout=2)
    assert report.outcome == "interrupted"
    assert calls(state_file)[-1] == [
        "interrupt",
        "--json",
        "--timeout",
        "2",
        session.hc_terminal_id,
    ]


def test_send_after_address_loss_uses_label_status(setup) -> None:
    """hc ls omits `reported`; the label fallback must enrich through get."""
    messaging, _service, store, _ws, state_file, session = setup
    store.conn.execute(
        "UPDATE sessions SET hc_terminal_id = NULL WHERE id = ?", (session.id,)
    )
    session.hc_terminal_id = None
    report = messaging.send(session, "hello")
    assert report.confirmed is True
    assert len(read_state(state_file)["sent"]) == 1
