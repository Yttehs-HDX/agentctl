"""Cleanup contracts: a refused git removal is reported, never assumed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import agentctl.agents.detector as detector
from agentctl.config import default_config
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.service import CreateRequest, SessionService
from agentctl.session.worktree import remove_worktree
from agentctl.storage import Store, open_connection
from agentctl.workspace.resolver import WorkspaceResolver


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path: Path, fake_hc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(detector, "installed", lambda d: d.id == "omp")
    monkeypatch.setattr(detector, "installed_command", lambda command: command[0] == "omp")
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


def test_remove_worktree_reports_git_failure(tmp_path: Path) -> None:
    removed, message = remove_worktree(
        repo_root=str(tmp_path / "missing-repo"),
        path=str(tmp_path / "worktree"),
        branch=None,
    )
    assert removed is False
    assert message


def test_delete_keeps_a_worktree_git_could_not_remove(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _store, ws, root = repo
    created = service.create(
        ws, CreateRequest(name="stuck", agent="omp", cwd=str(root), mode="worktree")
    )
    path = Path(created.session.worktree_path)
    monkeypatch.setattr(
        "agentctl.session.service.remove_worktree", lambda **kwargs: (False, "remove failed")
    )
    deleted = service.delete(ws, "stuck")
    assert "worktree kept" in (deleted.last_error or "")
    assert "remove failed" in (deleted.last_error or "")
    assert path.exists()


def test_failed_create_reports_a_worktree_it_could_not_remove(repo, monkeypatch) -> None:
    from agentctl.errors import RuntimeSpawnFailed

    service, _store, ws, root = repo
    monkeypatch.setattr(
        "agentctl.session.service.remove_worktree", lambda **kwargs: (False, "remove failed")
    )

    def fail(*args, **kwargs):
        raise RuntimeSpawnFailed("spawn failed")

    monkeypatch.setattr(service.runtime, "create_terminal", fail)
    with pytest.raises(RuntimeSpawnFailed) as excinfo:
        service.create(
            ws, CreateRequest(name="dead", agent="omp", cwd=str(root), mode="worktree")
        )
    assert "cleanup incomplete" in (excinfo.value.hint or "")
    assert "remove failed" in (excinfo.value.hint or "")
    assert list((root / ".agentctl" / "worktrees").iterdir())


def test_name_race_reports_a_worktree_it_could_not_remove(repo, monkeypatch) -> None:
    from agentctl.errors import SessionNameConflict

    service, store, ws, root = repo
    service.create(ws, CreateRequest(name="dup", agent="omp", cwd=str(root)))
    monkeypatch.setattr(store.sessions, "find_in_workspace", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "agentctl.session.service.remove_worktree", lambda **kwargs: (False, "remove failed")
    )
    with pytest.raises(SessionNameConflict) as excinfo:
        service.create(
            ws, CreateRequest(name="dup", agent="omp", cwd=str(root), mode="worktree")
        )
    assert "cleanup incomplete" in (excinfo.value.hint or "")
    assert "remove failed" in (excinfo.value.hint or "")
