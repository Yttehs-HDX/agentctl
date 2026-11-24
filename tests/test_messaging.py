"""Messaging: Herdr owns delivery, its guard, and the turn wait.

There is no agentctl message queue to test. What agentctl must get right is
identity (session → terminal), passing `--force` and `--wait` through, and
reporting Herdr's own `started` answer instead of inventing an acknowledgement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agentctl.agents.detector as detector
from agentctl.config import default_config
from agentctl.errors import RuntimeFailure, UsageError
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


def test_send_types_into_the_sessions_terminal(setup) -> None:
    messaging, _service, _store, _ws, state_file, session = setup
    report = messaging.send(session, "hello")
    assert report.terminal == session.hc_terminal_id
    assert report.started is True
    sent = read_state(state_file)["sent"][-1]
    assert sent["terminal"] == session.hc_terminal_id
    assert sent["text"] == "hello"


def test_send_adds_no_marker_of_its_own(setup) -> None:
    """Herdr delivers the text; agentctl must not rewrite the payload."""
    messaging, _service, _store, _ws, state_file, session = setup
    payload = "line one\nline two\n\tquoted \"x\" $VAR --flag ✓ 日本語"
    messaging.send(session, payload)
    assert read_state(state_file)["sent"][-1]["text"] == payload


def test_send_passes_force_through_to_herdr(setup) -> None:
    messaging, _service, _store, _ws, state_file, session = setup
    messaging.send(session, "answer the dialog", force=True)
    argv = calls(state_file)[-1]
    assert "send" in argv
    assert "--force" in argv


def test_force_is_absent_by_default(setup) -> None:
    messaging, _service, _store, _ws, state_file, session = setup
    messaging.send(session, "normal")
    assert "--force" not in calls(state_file)[-1]


def test_send_can_ask_herdr_to_wait_for_the_turn(setup) -> None:
    messaging, _service, _store, _ws, state_file, session = setup
    report = messaging.send(session, "work", wait=True, timeout=30)
    argv = calls(state_file)[-1]
    assert "--wait" in argv
    assert argv[argv.index("--timeout") + 1] == "30"
    assert report.wait is not None


def test_started_false_is_reported_honestly(setup) -> None:
    """Herdr says no turn began; that is a doubt, not a success."""
    messaging, _service, _store, _ws, state_file, session = setup
    state = read_state(state_file)
    state["send_started"] = False
    state_file.write_text(json.dumps(state))

    report = messaging.send(session, "boot")
    assert report.started is False
    assert report.to_dict()["started"] is False


def test_send_to_a_finished_session_is_refused(setup) -> None:
    messaging, _service, _store, _ws, state_file, session = setup
    state = read_state(state_file)
    for body in state["terminals"].values():
        body["state"] = "finished"
        body["reason"] = "exited"
    state_file.write_text(json.dumps(state))
    with pytest.raises(RuntimeFailure):
        messaging.send(session, "too late")


def test_send_to_a_missing_terminal_is_refused(setup) -> None:
    messaging, _service, _store, _ws, state_file, session = setup
    state = read_state(state_file)
    state["terminals"] = {}
    state_file.write_text(json.dumps(state))
    with pytest.raises(RuntimeFailure) as excinfo:
        messaging.send(session, "anyone there")
    assert "no runtime terminal" in excinfo.value.message


def test_keys_and_interrupt_resolve_the_terminal(setup) -> None:
    messaging, _service, _store, _ws, state_file, session = setup
    keys = messaging.keys(session, ["down", "enter"])
    assert keys.terminal == session.hc_terminal_id
    assert "label" not in calls(state_file)[-1]
    report = messaging.interrupt(session)
    assert report.key == "ctrl+c"


def test_key_names_never_start_with_a_dash(setup) -> None:
    messaging, _service, _store, _ws, _state, session = setup
    with pytest.raises(UsageError):
        messaging.keys(session, ["--help"])


def test_children_wait_uses_the_leaders_terminal(setup) -> None:
    messaging, _service, store, ws, state_file, _session = setup
    from agentctl.leader.service import LeaderService

    LeaderService(store, messaging.runtime).attach(ws, take_over=True)  # outside-Herdr Leader
    outcome = messaging.wait_children(leader=store.leaders.get(ws.id), mode="all", timeout=5)
    assert outcome.reason == "children"
    argv = calls(state_file)[-1]
    assert argv[argv.index("--children") + 1] == "all"


def test_children_wait_accepts_no_leader(setup) -> None:
    messaging, _service, _store, _ws, _state, _session = setup
    outcome = messaging.wait_children(leader=None, mode="any", timeout=5)
    assert outcome.reason == "children"
