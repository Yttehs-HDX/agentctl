"""Hook entry points: exact ACK binding, dedupe, and session checks."""

from __future__ import annotations

from pathlib import Path

import pytest

import agentctl.agents.detector as detector
from agentctl.config import default_config
from agentctl.errors import UsageError
from agentctl.events import EventBus
from agentctl.integrations.hooks import (
    claude_hook,
    pending_notice,
    record_message_received,
    record_session_started,
    record_turn_completed,
    resolve_workspace,
)
from agentctl.messaging.service import MessageService
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.lifecycle import LifecycleManager
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
    service = SessionService(store, HerdrCoreAdapter(), config)
    session = service.create(ws, CreateRequest(name="w1", agent="omp", cwd=str(workspace))).session
    other = service.create(ws, CreateRequest(name="w2", agent="omp", cwd=str(workspace))).session
    messages = MessageService(store, HerdrCoreAdapter(), LifecycleManager(store, HerdrCoreAdapter()), bus=EventBus(store))
    yield store, ws, session, other, messages
    conn.close()


def test_session_started_is_recorded_once(env) -> None:
    store, ws, session, _other, _messages = env
    record_session_started(store, ws, session.id, agent="omp")
    record_session_started(store, ws, session.id, agent="omp")
    ready = [e for e in EventBus(store).list(ws) if e.event == "session.ready"]
    assert len(ready) == 1


def test_ack_binds_to_the_messages_own_session(env) -> None:
    store, ws, session, other, messages = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    result = record_message_received(
        store, ws, session.id, message_id=message.id, source="omp"
    )
    assert result["acknowledged"] is True
    assert result["session"] == session.id
    assert store.messages.find(message.id).state == "running"


def test_ack_from_the_wrong_session_is_refused(env) -> None:
    store, ws, session, other, messages = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    with pytest.raises(UsageError) as excinfo:
        record_message_received(store, ws, other.id, message_id=message.id, source="omp")
    assert "belongs to session" in excinfo.value.message
    # A refused ACK must not have marked anything.
    assert store.messages.find(message.id).state == "dispatched"


def test_duplicate_hook_ack_reports_unchanged(env) -> None:
    store, ws, session, _other, messages = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    first = record_message_received(store, ws, session.id, message_id=message.id, source="omp")
    second = record_message_received(store, ws, session.id, message_id=message.id, source="omp")
    assert first["acknowledged"] is True
    assert second["acknowledged"] is False


def test_marker_without_a_known_message_is_refused(env) -> None:
    store, ws, session, _other, _messages = env
    with pytest.raises(UsageError):
        record_message_received(store, ws, session.id, message_id="msg_unknown", source="omp")


def test_no_marker_means_no_ack(env) -> None:
    store, ws, session, _other, _messages = env
    result = record_message_received(store, ws, session.id, message_id=None, source="omp")
    assert result["acknowledged"] is False
    assert "no agentctl marker" in result["reason"]


def test_turn_completion_closes_the_in_flight_message_without_an_ack(env) -> None:
    store, ws, session, _other, messages = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    result = record_turn_completed(
        store, ws, session.id, message_id=None, source="omp", result="done"
    )
    assert result["completed"] is True
    assert store.messages.find(message.id).state == "completed"


def test_turn_completion_is_idempotent(env) -> None:
    store, ws, session, _other, messages = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    record_turn_completed(store, ws, session.id, message_id=message.id, source="omp", result="a")
    record_turn_completed(store, ws, session.id, message_id=message.id, source="omp", result="a")
    completions = [e for e in EventBus(store).list(ws) if e.event == "turn.completed"]
    assert len(completions) == 1


def test_pending_notice_lists_what_a_leader_must_see(env) -> None:
    store, ws, session, _other, messages = env
    message = messages.enqueue(ws, session, "work")
    messages.dispatch(ws, message.id)
    messages.acknowledge(ws, message.id)
    messages.complete(ws, message.id, result_summary="found three issues")
    notice = pending_notice(store, ws)
    assert "[agentctl event]" in notice
    assert "worker=w1" in notice
    assert f"message={message.id}" in notice
    assert "turn.completed" in notice

    # Acknowledging clears it so the same news is never delivered twice.
    for item in EventBus(store).pending_for_leader(ws):
        store.inbox.ack(item.id, stamp="2026-01-01T00:00:00+00:00")
    assert pending_notice(store, ws) == ""


def test_claude_stop_hook_wakes_once_then_goes_quiet(env) -> None:
    store, ws, session, _other, messages = env
    message = messages.enqueue(ws, session, "review the code")
    messages.dispatch(ws, message.id)
    messages.acknowledge(ws, message.id)
    messages.complete(ws, message.id, result_summary="three issues")

    first = claude_hook(
        store,
        {"hook_event_name": "Stop", "last_assistant_message": "done"},
        env_workspace=ws.id,
        env_session=session.id,
    )
    assert first.get("decision") == "block"
    assert "[agentctl event]" in first["reason"]

    # Claude sets stop_hook_active on the follow-up Stop: never wake again.
    second = claude_hook(
        store,
        {"hook_event_name": "Stop", "stop_hook_active": True},
        env_workspace=ws.id,
        env_session=session.id,
    )
    assert "decision" not in second


def test_claude_user_prompt_submit_acks_its_marker(env) -> None:
    store, ws, session, _other, messages = env
    message = messages.enqueue(ws, session, "x")
    messages.dispatch(ws, message.id)
    result = claude_hook(
        store,
        {"hook_event_name": "UserPromptSubmit", "prompt": f"do it [[agentctl:{message.id}]]"},
        env_workspace=ws.id,
        env_session=session.id,
    )
    assert result["agentctl"]["acknowledged"] is True


def test_claude_hook_needs_a_known_workspace(env) -> None:
    store, _ws, session, _other, _messages = env
    with pytest.raises(UsageError):
        claude_hook(store, {"hook_event_name": "SessionStart"}, env_workspace=None, env_session=session.id)


def test_resolve_workspace_reports_a_clear_error(env) -> None:
    store, _ws, _session, _other, _messages = env
    with pytest.raises(UsageError):
        resolve_workspace(store, None)
    with pytest.raises(UsageError):
        resolve_workspace(store, "ws_missing")


def test_unbound_turn_completion_dedupes_on_content(env) -> None:
    """Claude's Stop hook can fire again with no message in flight.

    The same turn reported twice must not create two events, so the dedupe key
    cannot contain the clock.
    """
    store, ws, session, _other, _messages = env
    for _ in range(3):
        record_turn_completed(store, ws, session.id, message_id=None, source="claude", result="done")
    events = [e for e in EventBus(store).list(ws) if e.event == "turn.completed"]
    assert len(events) == 1


def test_distinct_unbound_turns_are_separate_events(env) -> None:
    store, ws, session, _other, _messages = env
    record_turn_completed(store, ws, session.id, message_id=None, source="claude", result="first")
    record_turn_completed(store, ws, session.id, message_id=None, source="claude", result="second")
    events = [e for e in EventBus(store).list(ws) if e.event == "turn.completed"]
    assert len(events) == 2
