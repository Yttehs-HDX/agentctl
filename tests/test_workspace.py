"""Workspace identity: canonical paths, stable ids, isolation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentctl.errors import InvalidPath, WorkspaceNotInitialized
from agentctl.storage import Store, open_database, open_connection
from agentctl.workspace.resolver import (
    WorkspaceResolver,
    canonical,
    filesystem_identity,
    is_inside,
    resolve_root,
    same_directory,
)


def _case_insensitive_fs(path: Path) -> bool:
    """Whether `path` lives on a filesystem that folds name case."""
    probe = path / "CaseProbe"
    probe.mkdir()
    try:
        return os.path.samefile(probe, path / "caseprobe")
    except OSError:
        return False


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


def test_rebind_moves_identity_without_changing_ids(workspace: Path, tmp_path: Path) -> None:
    from agentctl.errors import UsageError
    from agentctl.workspace.service import WorkspaceService

    moved = tmp_path / "moved"
    moved.mkdir()
    with open_database() as conn:
        store = Store(conn)
        service = WorkspaceService(store, runtime=None)
        original, _ = service.init(str(workspace))
        before = (original.id, original.leader_id)
        service.rebind(original, str(moved))
        after, created = service.init(str(moved))
    assert (after.id, after.leader_id) == before
    assert created is False
    assert after.root_path == str(moved)


def test_rebind_refuses_a_root_owned_by_another_workspace(workspace: Path, tmp_path: Path) -> None:
    from agentctl.errors import UsageError
    from agentctl.workspace.service import WorkspaceService

    other = tmp_path / "other"
    other.mkdir()
    with open_database() as conn:
        service = WorkspaceService(Store(conn), runtime=None)
        mine, _ = service.init(str(workspace))
        service.init(str(other))
        with pytest.raises(UsageError):
            service.rebind(mine, str(other))


def test_orphans_exclude_remote_terminals(workspace: Path) -> None:
    """A `machine/t7` terminal is never this workspace's orphan.

    `hc ls` lists this machine's terminals first and then each connected
    machine's; comparing bare addresses would report another machine's work as
    this workspace's leftover.
    """
    from agentctl.runtime.base import TerminalInfo
    from agentctl.workspace.service import WorkspaceService

    class StubRuntime:
        def available(self):
            return True

        def version(self):
            return "test"

        def list_terminals(self, *, labels=None, include_finished=False):
            return [
                TerminalInfo(
                    address="t2",
                    name="local-orphan",
                    state="live",
                    status="idle",
                    app=None,
                    cwd=str(workspace),
                    exit_code=None,
                    reason=None,
                    labels=dict(labels or {}),
                ),
                TerminalInfo(
                    address="boxb/t2",
                    name="remote",
                    state="live",
                    status="idle",
                    app=None,
                    cwd=str(workspace),
                    exit_code=None,
                    reason=None,
                    labels=dict(labels or {}),
                ),
            ]

    with open_database() as conn:
        store = Store(conn)
        service = WorkspaceService(store, StubRuntime())
        ws, _ = service.init(str(workspace))
        info = service.info(ws)
    assert info.orphan_terminals == ["t2"]


def test_is_local_distinguishes_machine_addresses() -> None:
    from agentctl.runtime.base import TerminalInfo

    def make(address: str) -> TerminalInfo:
        return TerminalInfo(
            address=address,
            name=None,
            state="live",
            status=None,
            app=None,
            cwd="/",
            exit_code=None,
            reason=None,
        )

    assert make("t7").is_local is True
    assert make("boxb/t7").is_local is False


def test_init_two_spellings_of_one_directory_share_an_identity(workspace: Path) -> None:
    if not _case_insensitive_fs(workspace.parent):
        pytest.skip("filesystem is case-sensitive")
    from agentctl.workspace.service import WorkspaceService

    variant = workspace.with_name(workspace.name.upper())
    assert str(variant) != str(workspace)
    with open_database() as conn:
        service = WorkspaceService(Store(conn), runtime=None)
        first, created = service.init(str(workspace))
        second, created_again = service.init(str(variant))
    assert created is True
    assert created_again is False
    assert second.id == first.id
    assert second.leader_id == first.leader_id


def test_discover_resolves_a_case_variant(workspace: Path) -> None:
    if not _case_insensitive_fs(workspace.parent):
        pytest.skip("filesystem is case-sensitive")
    from agentctl.workspace.discovery import discover

    variant = workspace.with_name(workspace.name.upper())
    with open_database() as conn:
        store = Store(conn)
        registered, _ = WorkspaceResolver(store).get_or_create(str(workspace))
        choice = discover(store, cwd=str(variant))
    assert choice.workspace.id == registered.id
    assert choice.source == "cwd"


def test_validate_cwd_respells_under_the_registered_root(workspace: Path) -> None:
    if not _case_insensitive_fs(workspace.parent):
        pytest.skip("filesystem is case-sensitive")
    from agentctl.session.service import SessionService

    variant = workspace.with_name(workspace.name.upper())
    sub = variant / "sub"
    sub.mkdir()
    with open_database() as conn:
        store = Store(conn)
        workspace_row, _ = WorkspaceResolver(store).get_or_create(str(workspace))
        service = SessionService(store, runtime=None, config=None)
        path = service.validate_cwd(workspace_row, str(sub))
    assert path.startswith(workspace_row.root_path)
    assert path == str(workspace / "sub")


def test_identity_ignores_spelling_noise(workspace: Path, tmp_path: Path) -> None:
    path = str(workspace)
    noisy = f"{path}/./"
    assert filesystem_identity(path) is not None
    assert filesystem_identity(path) == filesystem_identity(noisy)

    other = tmp_path / "other"
    other.mkdir()
    assert not same_directory(path, str(other))
    assert not same_directory(path, str(tmp_path / "missing"))
