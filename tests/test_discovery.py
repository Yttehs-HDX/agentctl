"""Workspace auto-detection: precedence, nesting, symlinks, clear failures."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentctl.cli import app
from agentctl.errors import WorkspaceNotInitialized
from agentctl.storage import Store, open_connection
from agentctl.workspace.discovery import discover, init_target
from agentctl.workspace.resolver import WorkspaceResolver, canonical

runner = CliRunner()


def _store(tmp_path: Path):
    conn = open_connection(tmp_path / "registry.db")
    return conn, Store(conn)


def _register(store, path: Path):
    return WorkspaceResolver(store).get_or_create(canonical(str(path)))[0]


def test_cwd_exact_match(tmp_path: Path) -> None:
    conn, store = _store(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    ws = _register(store, root)
    choice = discover(store, cwd=str(root))
    assert choice.workspace.id == ws.id
    assert choice.source == "cwd"
    conn.close()


def test_nearest_parent_match_for_nested_workspaces(tmp_path: Path) -> None:
    conn, store = _store(tmp_path)
    outer = tmp_path / "outer"
    inner = outer / "packages" / "inner"
    inner.mkdir(parents=True)
    deep = inner / "src" / "mod"
    deep.mkdir(parents=True)
    outer_ws = _register(store, outer)
    inner_ws = _register(store, inner)

    choice = discover(store, cwd=str(deep))
    assert choice.workspace.id == inner_ws.id, "the deepest registered root wins"
    assert choice.source == "parent"
    assert choice.workspace.id != outer_ws.id

    # The outer workspace still resolves from a directory outside the inner one.
    sibling = outer / "docs"
    sibling.mkdir()
    assert discover(store, cwd=str(sibling)).workspace.id == outer_ws.id
    conn.close()


def test_symlinked_cwd_resolves_to_the_real_root(tmp_path: Path) -> None:
    conn, store = _store(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    ws = _register(store, real)
    assert discover(store, cwd=str(link)).workspace.id == ws.id
    conn.close()


def test_flag_beats_environment_and_cwd(tmp_path: Path) -> None:
    conn, store = _store(tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    ws_a = _register(store, a)
    ws_b = _register(store, b)
    choice = discover(store, explicit=str(a), env=str(b), cwd=str(b))
    assert choice.workspace.id == ws_a.id
    assert choice.source == "flag"

    env_choice = discover(store, explicit=None, env=str(b), cwd=str(a))
    assert env_choice.workspace.id == ws_b.id
    assert env_choice.source == "env"
    conn.close()


def test_unregistered_directory_is_a_clear_error(tmp_path: Path) -> None:
    conn, store = _store(tmp_path)
    loose = tmp_path / "nothing-here"
    loose.mkdir()
    with pytest.raises(WorkspaceNotInitialized) as excinfo:
        discover(store, cwd=str(loose))
    assert "no registered workspace" in excinfo.value.message
    assert excinfo.value.hint and "agentctl init" in excinfo.value.hint
    conn.close()


def test_similar_prefix_is_not_matched(tmp_path: Path) -> None:
    conn, store = _store(tmp_path)
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    _register(store, app_dir)
    lookalike = tmp_path / "app-two"
    lookalike.mkdir()
    with pytest.raises(WorkspaceNotInitialized):
        discover(store, cwd=str(lookalike))
    conn.close()


def test_init_defaults_to_cwd(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    target.mkdir()
    root, source = init_target(cwd=str(target))
    assert root == canonical(str(target))
    assert source == "cwd"


def test_init_honours_flag_then_env(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert init_target(explicit=str(a), env=str(b), cwd=str(b)) == (canonical(str(a)), "flag")
    assert init_target(explicit=None, env=str(b), cwd=str(a)) == (canonical(str(b)), "env")


def test_cli_commands_work_without_explicit_workspace(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "backend").mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENTCTL_HOME", str(tmp_path / "home"))
    setup = runner.invoke(app, ["--json", "setup"])
    assert setup.exit_code == 0, setup.output

    init = runner.invoke(app, ["--json", "init"])
    assert init.exit_code == 0, init.output
    body = json.loads(init.output)
    assert body["created"] is True
    assert body["workspace"]["root"] == canonical(str(root))

    again = runner.invoke(app, ["--json", "init"])
    assert again.exit_code == 0
    assert json.loads(again.output)["created"] is False

    info = runner.invoke(app, ["--json", "info"])
    assert info.exit_code == 0, info.output
    assert json.loads(info.output)["id"] == body["workspace"]["id"]

    # From a subdirectory the parent workspace is found, not a new one.
    monkeypatch.chdir(root / "backend")
    listing = runner.invoke(app, ["--json", "list"])
    assert listing.exit_code == 0, listing.output
    assert json.loads(listing.output)["workspace_id"] == body["workspace"]["id"]


def test_cli_reports_when_no_workspace_is_registered(tmp_path: Path, monkeypatch) -> None:
    away = tmp_path / "away"
    away.mkdir()
    monkeypatch.chdir(away)
    monkeypatch.setenv("AGENTCTL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AGENTCTL_WORKSPACE", raising=False)
    build = runner.invoke(app, ["--json", "setup"])
    assert build.exit_code == 0, build.output
    result = runner.invoke(app, ["--json", "info"])
    assert isinstance(result.exception, WorkspaceNotInitialized), result.output
    assert result.exception.exit_code == 4
    assert result.exception.code == "workspace_not_initialized"


def test_env_workspace_is_used_when_cwd_is_elsewhere(tmp_path: Path, monkeypatch) -> None:
    conn, store = _store(tmp_path)
    registered = tmp_path / "registered"
    registered.mkdir()
    ws = _register(store, registered)
    conn.close()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("AGENTCTL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTCTL_WORKSPACE", str(registered))
    runner.invoke(app, ["--json", "setup"])
    info = runner.invoke(app, ["--json", "info"])
    assert info.exit_code == 0, info.output
    assert json.loads(info.output)["id"] == ws.id
