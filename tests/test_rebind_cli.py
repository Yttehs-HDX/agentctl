"""Rebind after a real `mv`, through the CLI, from the path that moved."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agentctl.agents.detector as detector
from agentctl.config import default_config, save_config
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.service import CreateRequest, SessionService
from agentctl.storage import Store, open_connection
from agentctl.workspace.resolver import WorkspaceResolver


def test_rebind_after_mv_from_the_old_path(
    tmp_path: Path, fake_hc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from agentctl.cli import app

    monkeypatch.setattr(detector, "installed", lambda d: d.id == "omp")
    monkeypatch.setattr(detector, "installed_command", lambda command: command[0] == "omp")
    save_config(default_config())

    root = tmp_path / "proj"
    root.mkdir()
    (root / "sub").mkdir()
    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(root))
    config = default_config()
    config.defaults.agent = "omp"
    created = SessionService(store, HerdrCoreAdapter(), config).create(
        ws, CreateRequest(name="w", agent="omp", cwd=str(root / "sub"))
    )
    conn.close()

    moved = tmp_path / "moved"
    root.rename(moved)

    result = CliRunner().invoke(
        app, ["--json", "--workspace", str(root), "rebind", str(moved)]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["workspace"]["root"] == str(moved)

    conn = open_connection()
    session = Store(conn).sessions.find_by_id(created.session.id)
    conn.close()
    assert session is not None
    assert session.cwd == str(moved / "sub")


def test_rebind_relocates_a_native_session_dir_under_the_old_root(tmp_path: Path) -> None:
    from agentctl.storage.models import Session
    from agentctl.workspace.service import WorkspaceService

    root = tmp_path / "proj"
    root.mkdir()
    native = root / "native"
    native.mkdir()
    session_id = "sess_" + "b" * 32
    stamp = "2026-01-01T00:00:00+00:00"
    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(root))
    store.sessions.insert(
        Session(
            id=session_id,
            workspace_id=ws.id,
            name="w",
            agent="omp",
            cwd=str(root),
            mode="shared",
            hc_terminal_id=None,
            hc_name="ac-w",
            native_session_id=None,
            native_session_dir=str(native),
            worktree_path=None,
            worktree_branch=None,
            git_root=None,
            runtime_started_at=None,
            status="stopped",
            last_error=None,
            created_at=stamp,
            updated_at=stamp,
            deleted_at=None,
        )
    )
    moved = tmp_path / "moved"
    root.rename(moved)

    WorkspaceService(store, None).rebind(ws, str(moved))

    stored = store.sessions.find_by_id(session_id)
    conn.close()
    assert stored is not None
    assert stored.cwd == str(moved)
    assert stored.native_session_dir == str(moved / "native")
    assert (moved / "native").is_dir()
