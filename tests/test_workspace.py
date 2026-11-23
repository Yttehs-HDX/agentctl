"""Workspace identity: canonical paths, stable ids, isolation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentctl.errors import InvalidPath, WorkspaceNotInitialized
from agentctl.storage import Store, open_database, open_connection
from agentctl.workspace.resolver import WorkspaceResolver, canonical, is_inside, resolve_root


def test_same_path_resolves_to_same_workspace(workspace: Path) -> None:
    with open_database() as conn:
        resolver = WorkspaceResolver(Store(conn))
        first, created = resolver.get_or_create(str(workspace))
        second, created_again = resolver.get_or_create(str(workspace))
    assert created is True
    assert created_again is False
    assert first.id == second.id
    assert first.leader_id == second.leader_id


def test_different_paths_get_different_workspaces(workspace: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    with open_database() as conn:
        resolver = WorkspaceResolver(Store(conn))
        a, _ = resolver.get_or_create(str(workspace))
        b, _ = resolver.get_or_create(str(other))
    assert a.id != b.id
    assert a.leader_id != b.leader_id


def test_identity_survives_reopen(workspace: Path) -> None:
    conn = open_connection()
    try:
        original, _ = WorkspaceResolver(Store(conn)).get_or_create(str(workspace))
    finally:
        conn.close()
    conn = open_connection()
    try:
        again = WorkspaceResolver(Store(conn)).require(str(workspace))
    finally:
        conn.close()
    assert again.id == original.id
    assert again.leader_id == original.leader_id


def test_symlink_resolves_to_the_same_identity(workspace: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(workspace)
    with open_database() as conn:
        resolver = WorkspaceResolver(Store(conn))
        direct, _ = resolver.get_or_create(str(workspace))
        via_link, created = resolver.get_or_create(canonical(str(link)))
    assert created is False
    assert via_link.id == direct.id


def test_relative_and_trailing_slash_are_canonical(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(workspace.parent)
    a = resolve_root(f"{workspace.name}/")
    b = resolve_root(str(workspace))
    assert a == b == str(workspace)


def test_missing_workspace_raises_not_initialized(tmp_path: Path) -> None:
    with open_database() as conn:
        resolver = WorkspaceResolver(Store(conn))
        with pytest.raises(WorkspaceNotInitialized):
            resolver.require(str(tmp_path))


def test_nonexistent_workspace_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidPath):
        resolve_root(str(tmp_path / "missing"))


def test_missing_workspace_flag_is_rejected() -> None:
    with pytest.raises(InvalidPath):
        resolve_root(None)


def test_is_inside_blocks_escape_and_sibling_prefixes(workspace: Path, tmp_path: Path) -> None:
    inside = workspace / "backend"
    assert is_inside(str(inside), str(workspace))
    assert is_inside(str(workspace), str(workspace))
    assert not is_inside(str(tmp_path / "project-evil"), str(workspace))
    assert not is_inside(str(tmp_path), str(workspace))


def test_dotdot_escape_is_canonicalised_away(workspace: Path) -> None:
    escaped = Path(os.path.normpath(str(workspace / ".." / "elsewhere")))
    resolved = os.path.realpath(str(workspace / ".." / "elsewhere"))
    assert str(escaped) == resolved
    assert not is_inside(resolved, str(workspace))
