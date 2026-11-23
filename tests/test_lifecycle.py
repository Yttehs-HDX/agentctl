"""Lifecycle: status derivation, terminal rediscovery, recovery rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentctl.agents.definitions import AGENT_DEFINITIONS
from agentctl.config import default_config
from agentctl.errors import RuntimeFailure
from agentctl.runtime.base import RuntimeAdapter, TerminalInfo
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.lifecycle import derive_status
from agentctl.session.service import CreateRequest, SessionService
from agentctl.storage import Store, open_connection
from agentctl.storage.models import Session
from agentctl.workspace.resolver import WorkspaceResolver
import agentctl.agents.detector as detector


def make_session(**overrides) -> Session:
    defaults = dict(
        id="sess_x",
        workspace_id="ws_x",
        name="w",
        agent="omp",
        cwd="/tmp",
        mode="shared",
        hc_terminal_id="t1",
        hc_name="ac-w-1",
        native_session_id=None,
        native_session_dir=None,
        worktree_path=None,
        worktree_branch=None,
        git_root=None,
        runtime_started_at=None,
        status="starting",
        last_error=None,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        deleted_at=None,
    )
    defaults.update(overrides)
    return Session(**defaults)


def terminal(**overrides) -> TerminalInfo:
    base = dict(
        address="t1",
        name="ac-w-1",
        state="live",
        status="idle",
        app="omp",
        cwd="/tmp",
        exit_code=None,
        reason=None,
        labels={},
        reported=True,
        raw={},
    )
    base.update(overrides)
    return TerminalInfo(**base)


def test_status_mapping_for_reporting_agent() -> None:
    session = make_session()
    cases = {
        "idle": "idle",
        "working": "working",
        "done": "completed",
        "blocked": "waiting",
        "error": "failed",
    }
    for word, expected in cases.items():
        status, _ = derive_status(session, terminal(status=word), integration=True, created_recently=False)
        assert status == expected, word


def test_missing_terminal_is_unknown() -> None:
    status, why = derive_status(make_session(), None, integration=True, created_recently=False)
    assert status == "unknown"
    assert why and "not found" in why


def test_non_reporting_agent_is_never_guessed() -> None:
    session = make_session(agent="codex")
    status, why = derive_status(session, terminal(status=None, app=None), integration=False, created_recently=False)
    assert status == "unknown"
    assert why == "agent does not report status"


def test_freshly_created_agent_is_starting() -> None:
    session = make_session()
    status, _ = derive_status(session, terminal(status=None), integration=False, created_recently=True)
    assert status == "starting"


def test_killed_terminal_is_stopped() -> None:
    status, _ = derive_status(
        make_session(), terminal(state="finished", status=None, reason="killed"), integration=True, created_recently=False
    )
    assert status == "stopped"


def test_failed_exit_code_is_failed() -> None:
    status, why = derive_status(
        make_session(),
        terminal(state="finished", status=None, exit_code=2, reason="exited"),
        integration=True,
        created_recently=False,
    )
    assert status == "failed"
    assert why and "code 2" in why


def test_clean_exit_is_stopped() -> None:
    status, _ = derive_status(
        make_session(),
        terminal(state="finished", status=None, exit_code=0, reason="exited"),
        integration=True,
        created_recently=False,
    )
    assert status == "stopped"


def test_unknown_status_word_is_not_coerced() -> None:
    status, why = derive_status(make_session(), terminal(status="looping"), integration=True, created_recently=False)
    assert status == "unknown"
    assert why and "looping" in why


@pytest.fixture()
def live(workspace: Path, fake_hc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(detector, "installed", lambda d: d.id == "omp")
    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(workspace))
    config = default_config()
    config.defaults.agent = "omp"
    service = SessionService(store, HerdrCoreAdapter(), config)
    yield service, store, ws
    conn.close()


def test_terminal_rediscovered_by_label(live, workspace: Path) -> None:
    service, store, ws = live
    created = service.create(ws, CreateRequest(name="relabel", agent=None, cwd=str(workspace)))
    # Simulate a leader that lost the persisted terminal id.
    store.sessions.update_runtime(
        created.session.id,
        hc_terminal_id=None,
        hc_name=created.session.hc_name,
        native_session_id=None,
        native_session_dir=None,
        status="unknown",
        last_error=None,
        updated_at=created.session.updated_at,
    )
    target = service.get(ws, "relabel")
    terminal_info = service.lifecycle.find_terminal(target)
    assert terminal_info is not None
    assert terminal_info.address == created.terminal.address
    service.lifecycle.refresh(target)
    assert store.sessions.find_by_id(target.id).hc_terminal_id == created.terminal.address


def test_refresh_marks_vanished_terminal(live, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, store, ws = live
    created = service.create(ws, CreateRequest(name="ghost", agent=None, cwd=str(workspace)))
    store.conn.execute("DELETE FROM sessions WHERE 0")  # no-op keeps sqlite connection warm
    monkeypatch.setattr(service.lifecycle.runtime, "find_terminal", lambda t: None)
    monkeypatch.setattr(service.lifecycle.runtime, "list_terminals", lambda **kw: [])
    target = service.get(ws, "ghost")
    service.lifecycle.refresh(target)
    assert target.status == "unknown"
    assert "not found" in (target.last_error or "")


def test_unrecoverable_session_keeps_its_record(live, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, store, ws = live
    created = service.create(ws, CreateRequest(name="lost", agent=None, cwd=str(workspace)))
    monkeypatch.setattr(service.lifecycle.runtime, "find_terminal", lambda t: None)
    monkeypatch.setattr(service.lifecycle.runtime, "list_terminals", lambda **kw: [])
    # No native id was captured for this fake run -> cannot resume.
    monkeypatch.setattr(service, "sync_native_id", lambda s: None)
    target, outcome = service.recover(ws, "lost")
    assert outcome == "unrecoverable"
    assert target.status == "unreachable"
    stored = store.sessions.find_by_id(created.session.id)
    assert stored is not None
    assert stored.status == "unreachable"


def test_recover_attaches_to_live_terminal(live, workspace: Path) -> None:
    service, _store, ws = live
    service.create(ws, CreateRequest(name="alive", agent=None, cwd=str(workspace)))
    target, outcome = service.recover(ws, "alive")
    assert outcome == "attached"
    assert target.status in {"idle", "starting", "completed"}


def test_recover_resumes_from_native_id(live, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, store, ws = live
    created = service.create(ws, CreateRequest(name="resumable", agent=None, cwd=str(workspace)))
    native = "01a209e7-c15b-701c-9eb3-65cab792e244"
    store.sessions.update_runtime(
        created.session.id,
        hc_terminal_id=None,
        hc_name=created.session.hc_name,
        native_session_id=native,
        native_session_dir=created.session.native_session_dir,
        status="stopped",
        last_error=None,
        updated_at=created.session.updated_at,
    )
    monkeypatch.setattr(service.lifecycle.runtime, "find_terminal", lambda t: None)
    monkeypatch.setattr(service.lifecycle.runtime, "list_terminals", lambda **kw: [])
    calls: list[list[str]] = []
    original = service.runtime.create_terminal

    def spy(argv, **kwargs):
        calls.append(argv)
        return original(argv, **kwargs)

    monkeypatch.setattr(service.runtime, "create_terminal", spy)
    target, outcome = service.recover(ws, "resumable")
    assert outcome == "resumed"
    assert "--resume" in calls[0]
    assert native in calls[0]
    assert target.hc_terminal_id


def test_non_resumable_agent_is_marked_unreachable(live, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, store, ws = live
    created = service.create(ws, CreateRequest(name="codexish", agent=None, cwd=str(workspace)))
    store.conn.execute("UPDATE sessions SET agent='codex' WHERE id=?", (created.session.id,))
    monkeypatch.setattr(service.lifecycle.runtime, "find_terminal", lambda t: None)
    monkeypatch.setattr(service.lifecycle.runtime, "list_terminals", lambda **kw: [])
    target, outcome = service.recover(ws, "codexish")
    assert outcome == "unrecoverable"
    assert target.status == "unreachable"
