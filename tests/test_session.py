"""Session service: creation, validation, rollback, deletion."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentctl.config import default_config
from agentctl.errors import (
    AgentNotConfigured,
    AgentUnknown,
    InvalidPath,
    SessionNameConflict,
    SessionNotFound,
)
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.service import CreateRequest, SessionService
from agentctl.storage import Store, open_connection
from agentctl.workspace.resolver import WorkspaceResolver
import agentctl.agents.detector as detector


@pytest.fixture()
def setup(workspace: Path, fake_hc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(detector, "installed", lambda d: d.id == "omp")
    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(workspace))
    config = default_config()
    config.defaults.agent = "omp"
    service = SessionService(store, HerdrCoreAdapter(), config)
    yield service, store, ws, config
    conn.close()


def test_create_records_identity_and_terminal(setup, workspace: Path) -> None:
    service, store, ws, _config = setup
    result = service.create(ws, CreateRequest(name="boot", agent=None, cwd=str(workspace / "backend")))
    session = result.session
    assert session.id.startswith("sess_")
    assert session.workspace_id == ws.id
    assert session.agent == "omp"
    assert session.cwd == str(workspace / "backend")
    assert session.mode == "shared"
    assert result.terminal.address
    assert session.hc_terminal_id == result.terminal.address
    stored = store.sessions.find_by_id(session.id)
    assert stored is not None
    assert stored.hc_terminal_id == session.hc_terminal_id


def test_session_id_never_equals_terminal_id(setup, workspace: Path) -> None:
    service, _store, ws, _config = setup
    result = service.create(ws, CreateRequest(name="ids", agent="omp", cwd=str(workspace)))
    assert result.session.id != result.terminal.address
    assert not result.session.id.startswith("t")


def test_duplicate_name_is_rejected(setup, workspace: Path) -> None:
    service, _store, ws, _config = setup
    service.create(ws, CreateRequest(name="dup", agent=None, cwd=str(workspace)))
    with pytest.raises(SessionNameConflict):
        service.create(ws, CreateRequest(name="dup", agent=None, cwd=str(workspace)))


def test_same_name_allowed_in_another_workspace(setup, tmp_path: Path, workspace: Path) -> None:
    service, store, ws, _config = setup
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = WorkspaceResolver(store).get_or_create(str(other_root))[0]
    service.create(ws, CreateRequest(name="same", agent=None, cwd=str(workspace)))
    service.create(other, CreateRequest(name="same", agent=None, cwd=str(other_root)))
    assert len(store.sessions.list_for_workspace(ws.id)) == 1


def test_cwd_outside_workspace_is_rejected(setup, tmp_path: Path) -> None:
    service, _store, ws, _config = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(InvalidPath):
        service.create(ws, CreateRequest(name="escape", agent=None, cwd=str(outside)))


def test_cwd_symlink_escape_is_rejected(setup, workspace: Path, tmp_path: Path) -> None:
    service, _store, ws, _config = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = workspace / "escape"
    escape.symlink_to(outside)
    with pytest.raises(InvalidPath):
        service.create(ws, CreateRequest(name="link", agent=None, cwd=str(escape)))


def test_missing_cwd_is_rejected(setup, workspace: Path) -> None:
    service, _store, ws, _config = setup
    with pytest.raises(InvalidPath):
        service.create(ws, CreateRequest(name="gone", agent=None, cwd=str(workspace / "nope")))


def test_no_agent_and_no_default_errors(setup, workspace: Path) -> None:
    service, _store, ws, config = setup
    config.defaults.agent = ""
    with pytest.raises(AgentNotConfigured):
        service.create(ws, CreateRequest(name="noagent", agent=None, cwd=str(workspace)))


def test_workspace_default_beats_global(setup, workspace: Path) -> None:
    service, store, ws, config = setup
    config.defaults.agent = "omp"
    ws.default_agent = "omp"
    assert service.resolve_agent(ws, None) == "omp"
    ws.default_agent = None


def test_explicit_agent_does_not_change_default(setup, workspace: Path) -> None:
    service, _store, ws, config = setup
    service.create(ws, CreateRequest(name="override", agent="omp", cwd=str(workspace)))
    assert config.defaults.agent == "omp"


def test_unknown_agent_rejected(setup, workspace: Path) -> None:
    service, _store, ws, _config = setup
    with pytest.raises(AgentUnknown):
        service.create(ws, CreateRequest(name="x", agent="ghost", cwd=str(workspace)))


def test_create_rolls_back_when_runtime_fails(
    setup, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentctl.errors import RuntimeFailure

    service, store, ws, _config = setup

    def boom(*args, **kwargs):
        raise RuntimeFailure("spawn failed", kind="spawn_failed")

    monkeypatch.setattr(HerdrCoreAdapter, "create_terminal", boom)
    with pytest.raises(RuntimeFailure):
        service.create(ws, CreateRequest(name="broken", agent=None, cwd=str(workspace)))
    assert store.sessions.find_in_workspace(ws.id, "broken") is None
    assert store.sessions.list_for_workspace(ws.id) == []


def test_delete_kills_and_retires(setup, workspace: Path) -> None:
    service, store, ws, _config = setup
    result = service.create(ws, CreateRequest(name="victim", agent=None, cwd=str(workspace)))
    service.delete(ws, "victim")
    assert store.sessions.find_in_workspace(ws.id, "victim") is None
    history = store.sessions.list_for_workspace(ws.id, include_deleted=True)
    assert history[0].id == result.session.id
    assert history[0].deleted_at is not None


def test_delete_unknown_session_raises(setup) -> None:
    service, _store, ws, _config = setup
    with pytest.raises(SessionNotFound):
        service.delete(ws, "nothing")


def test_get_is_scoped_to_workspace(setup, workspace: Path, tmp_path: Path) -> None:
    service, store, ws, _config = setup
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = WorkspaceResolver(store).get_or_create(str(other_root))[0]
    service.create(other, CreateRequest(name="theirs", agent=None, cwd=str(other_root)))
    with pytest.raises(SessionNotFound):
        service.get(ws, "theirs")


def test_names_are_reusable_after_delete(setup, workspace: Path) -> None:
    service, _store, ws, _config = setup
    service.create(ws, CreateRequest(name="cycle", agent=None, cwd=str(workspace)))
    service.delete(ws, "cycle")
    service.create(ws, CreateRequest(name="cycle", agent=None, cwd=str(workspace)))
    assert service.get(ws, "cycle").name == "cycle"


def test_hc_name_is_valid_for_the_runtime(setup, workspace: Path) -> None:
    import re

    service, _store, ws, _config = setup
    result = service.create(ws, CreateRequest(name="My Weird/Name", agent=None, cwd=str(workspace)))
    name = result.session.hc_name
    assert name is not None
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", name)
    assert len(name) <= 32
