"""Prune: reclaim agentctl-owned state for soft-deleted sessions."""

from __future__ import annotations

from pathlib import Path

import pytest

import agentctl.agents.detector as detector
from agentctl.config import default_config
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.service import CreateRequest, SessionService
from agentctl.storage import Store, open_connection
from agentctl.workspace.resolver import WorkspaceResolver


@pytest.fixture()
def setup(workspace: Path, fake_hc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(detector, "installed", lambda definition: True)
    monkeypatch.setattr(detector, "installed_command", lambda command: True)
    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(workspace))
    config = default_config()
    config.defaults.agent = "omp"
    service = SessionService(store, HerdrCoreAdapter(), config)
    session = service.create(
        ws, CreateRequest(name="gone", agent="omp", cwd=str(workspace))
    ).session
    directory = Path(session.native_session_dir or "")
    (directory / "log.jsonl").write_bytes(b"x" * 2048)
    service.delete(ws, "gone")
    yield service, store, ws, session, directory
    conn.close()


def test_dry_run_reports_without_deleting(setup) -> None:
    service, store, ws, session, directory = setup
    result = service.prune(ws, dry_run=True)
    assert result.sessions == 1
    assert result.directories == 1
    assert result.bytes_freed > 0
    assert result.dry_run is True
    assert directory.exists()
    assert store.sessions.find_by_id(session.id) is not None


def test_real_prune_removes_directory_and_row(setup) -> None:
    service, store, ws, session, directory = setup
    result = service.prune(ws)
    assert result.dry_run is False
    assert result.sessions == 1
    assert result.directories == 1
    assert result.bytes_freed >= 2048
    assert not directory.exists()
    assert store.sessions.find_by_id(session.id) is None


def test_live_session_is_untouched(setup, workspace: Path) -> None:
    service, store, _ws, session, directory = setup
    live = service.create(
        _ws, CreateRequest(name="alive", agent="omp", cwd=str(workspace))
    ).session
    live_dir = Path(live.native_session_dir or "")
    service.prune(_ws)
    assert store.sessions.find_by_id(live.id) is not None
    assert live_dir.exists()
    assert not directory.exists()
    assert store.sessions.find_by_id(session.id) is None


def test_a_recorded_path_cannot_redirect_prune(setup, workspace: Path) -> None:
    """The row's path is a claim; deletion follows the session id."""
    service, store, ws, session, directory = setup
    outside = workspace / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    store.conn.execute(
        "UPDATE sessions SET native_session_dir = ? WHERE id = ?", (str(outside), session.id)
    )
    result = service.prune(ws)
    assert outside.exists()
    assert not directory.exists()
    assert store.sessions.find_by_id(session.id) is None
    assert result.notes


def test_a_deleted_row_cannot_delete_another_sessions_directory(setup, workspace: Path) -> None:
    service, store, ws, session, directory = setup
    other = service.create(ws, CreateRequest(name="live", agent="omp", cwd=str(workspace))).session
    other_dir = Path(other.native_session_dir or "")
    store.conn.execute(
        "UPDATE sessions SET native_session_dir = ? WHERE id = ?", (str(other_dir), session.id)
    )
    service.prune(ws)
    assert other_dir.exists()
    assert store.sessions.find_by_id(other.id) is not None
    assert not directory.exists()


def test_unsafe_session_id_is_refused(setup) -> None:
    service, store, ws, session, directory = setup
    store.conn.execute("UPDATE sessions SET id = ? WHERE id = ?", ("../escape", session.id))
    result = service.prune(ws)
    assert result.errors
    assert directory.exists()


def test_missing_session_directory_still_prunes_row(setup) -> None:
    import shutil

    service, store, ws, session, directory = setup
    shutil.rmtree(directory)
    result = service.prune(ws)
    assert store.sessions.find_by_id(session.id) is None
    assert result.sessions == 1
    assert result.directories == 0
    assert result.bytes_freed == 0


def test_a_nested_claim_is_ignored(setup) -> None:
    service, store, ws, session, directory = setup
    nested = directory / "nested"
    nested.mkdir()
    store.conn.execute(
        "UPDATE sessions SET native_session_dir = ? WHERE id = ?", (str(nested), session.id)
    )
    result = service.prune(ws)
    assert not directory.exists()
    assert store.sessions.find_by_id(session.id) is None
    assert result.notes


def test_a_symlinked_session_directory_is_refused(setup, workspace: Path) -> None:
    import shutil

    service, store, ws, session, directory = setup
    other = service.create(ws, CreateRequest(name="live", agent="omp", cwd=str(workspace))).session
    other_dir = Path(other.native_session_dir or "")
    shutil.rmtree(directory)
    directory.symlink_to(other_dir, target_is_directory=True)
    result = service.prune(ws)
    assert result.errors
    assert other_dir.exists()
    assert store.sessions.find_by_id(other.id) is not None
