"""Worktree mode: isolation, cleanup policy, non-git rejection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentctl.config import default_config
from agentctl.errors import InvalidPath
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.service import CreateRequest, SessionService
from agentctl.session.worktree import worktree_is_clean
from agentctl.storage import Store, open_connection
from agentctl.workspace.resolver import WorkspaceResolver
import agentctl.agents.detector as detector


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path: Path, fake_hc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(detector, "installed", lambda d: d.id == "omp")
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "-q", ".", cwd=root)
    git("config", "user.email", "t@example.com", cwd=root)
    git("config", "user.name", "t", cwd=root)
    (root / "a.txt").write_text("hello\n")
    git("add", "-A", cwd=root)
    git("commit", "-qm", "init", cwd=root)

    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(root))
    config = default_config()
    config.defaults.agent = "omp"
    service = SessionService(store, HerdrCoreAdapter(), config)
    yield service, store, ws, root
    conn.close()


def test_worktree_is_created_inside_workspace(repo) -> None:
    service, _store, ws, root = repo
    result = service.create(ws, CreateRequest(name="wt", agent=None, cwd=str(root), mode="worktree"))
    session = result.session
    assert session.mode == "worktree"
    assert session.worktree_path is not None
    worktree = Path(session.worktree_path)
    assert worktree.is_dir()
    assert worktree.parent.parent == root / ".agentctl"
    assert session.cwd == session.worktree_path
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=root, capture_output=True, text=True
    ).stdout
    assert str(worktree) in listing


def test_worktree_branch_is_recorded(repo) -> None:
    service, _store, ws, root = repo
    result = service.create(ws, CreateRequest(name="br", agent=None, cwd=str(root), mode="worktree"))
    assert result.session.worktree_branch == "agentctl/br"
    branches = subprocess.run(
        ["git", "branch", "--list"], cwd=root, capture_output=True, text=True
    ).stdout
    assert "agentctl/br" in branches


def test_worktree_dir_is_excluded_from_main_status(repo) -> None:
    service, _store, ws, root = repo
    service.create(ws, CreateRequest(name="ex", agent=None, cwd=str(root), mode="worktree"))
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
    ).stdout
    assert ".agentctl" not in status


def test_delete_removes_clean_worktree_and_branch(repo) -> None:
    service, _store, ws, root = repo
    result = service.create(ws, CreateRequest(name="clean", agent=None, cwd=str(root), mode="worktree"))
    path = result.session.worktree_path
    service.delete(ws, "clean")
    assert not Path(path).exists()
    branches = subprocess.run(
        ["git", "branch", "--list"], cwd=root, capture_output=True, text=True
    ).stdout
    assert "agentctl/clean" not in branches


def test_delete_keeps_worktree_with_uncommitted_work(repo) -> None:
    service, store, ws, root = repo
    result = service.create(ws, CreateRequest(name="dirty", agent=None, cwd=str(root), mode="worktree"))
    path = Path(result.session.worktree_path)
    (path / "output.txt").write_text("worker result\n")
    kept = service.delete(ws, "dirty")
    assert path.is_dir()
    assert (path / "output.txt").read_text() == "worker result\n"
    assert "uncommitted" in (kept.last_error or "")
    row = store.sessions.find_by_id(result.session.id)
    assert row is not None and "worktree kept" in (row.last_error or "")


def test_keep_worktree_flag_skips_removal(repo) -> None:
    service, _store, ws, root = repo
    result = service.create(ws, CreateRequest(name="manual", agent=None, cwd=str(root), mode="worktree"))
    path = result.session.worktree_path
    service.delete(ws, "manual", keep_worktree=True)
    assert Path(path).is_dir()


def test_non_git_directory_is_rejected(tmp_path: Path, fake_hc: Path, monkeypatch) -> None:
    monkeypatch.setattr(detector, "installed", lambda d: d.id == "omp")
    plain = tmp_path / "plain"
    plain.mkdir()
    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(plain))
    config = default_config()
    config.defaults.agent = "omp"
    service = SessionService(store, HerdrCoreAdapter(), config)
    with pytest.raises(InvalidPath):
        service.create(ws, CreateRequest(name="nogit", agent=None, cwd=str(plain), mode="worktree"))
    assert store.sessions.list_for_workspace(ws.id) == []
    conn.close()


def test_worktree_is_clean_detects_changes(repo) -> None:
    service, _store, ws, root = repo
    result = service.create(ws, CreateRequest(name="check", agent=None, cwd=str(root), mode="worktree"))
    path = result.session.worktree_path
    assert worktree_is_clean(path) is True
    (Path(path) / "new.txt").write_text("x")
    assert worktree_is_clean(path) is False
