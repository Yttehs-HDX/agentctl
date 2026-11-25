"""Workspace-level operations: init, info, listing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentctl.errors import AgentctlError, RuntimeFailure, UsageError
from agentctl.leader.service import LeaderService
from agentctl.runtime.base import RuntimeAdapter
from agentctl.session.worktree import repair_worktree
from agentctl.storage import transaction
from agentctl.storage.models import Session, Workspace
from agentctl.workspace.resolver import WorkspaceResolver, is_inside, now


@dataclass(slots=True)
class WorkspaceInfo:
    workspace: Workspace
    session_count: int
    active_count: int
    default_agent: str | None
    runtime_version: str | None
    runtime_available: bool
    orphan_terminals: list[str]

    def to_dict(self) -> dict[str, object]:
        body = self.workspace.to_dict()
        body.update(
            {
                "sessions_total": self.session_count,
                "sessions_active": self.active_count,
                "default_agent": self.default_agent,
                "runtime": {
                    "available": self.runtime_available,
                    "version": self.runtime_version,
                    "orphan_terminals": self.orphan_terminals,
                },
            }
        )
        return body


class WorkspaceService:
    def __init__(self, store, runtime: RuntimeAdapter, config=None) -> None:
        self.store = store
        self.runtime = runtime
        self.config = config
        self.resolver = WorkspaceResolver(store)

    def init(self, root: str) -> tuple[Workspace, bool]:
        return self.resolver.get_or_create(root)

    def require(self, root: str) -> Workspace:
        return self.resolver.require(root)

    def sessions(self, workspace: Workspace) -> list[Session]:
        return self.store.sessions.list_for_workspace(workspace.id)

    def info(self, workspace: Workspace) -> WorkspaceInfo:
        sessions = self.sessions(workspace)
        active = [s for s in sessions if s.status in {"starting", "idle", "working", "waiting", "completed"}]
        runtime_available = self.runtime.available()
        version = None
        if runtime_available:
            try:
                version = self.runtime.version()
            except AgentctlError:
                version = None
        orphans: list[str] = []
        if runtime_available:
            try:
                terminals = self.runtime.list_terminals(labels={"agentctl.workspace": workspace.id})
            except AgentctlError:
                terminals = []
            known = {s.hc_terminal_id for s in sessions if s.hc_terminal_id}
            # Remote terminals (`machine/t7`) are never agentctl's: this
            # process only creates local ones, so listing them as orphans would
            # blame other machines' work on this workspace.
            if terminals:
                try:
                    leader = LeaderService(self.store, self.runtime).status(workspace)
                except RuntimeFailure:
                    pass
                else:
                    if leader.live and leader.terminal is not None:
                        known.add(leader.terminal.address)
            orphans = [t.address for t in terminals if t.is_local and t.address not in known]
        effective_default = workspace.default_agent or (
            self.config.defaults.agent if self.config is not None and self.config.defaults.agent else None
        )
        return WorkspaceInfo(
            workspace=workspace,
            session_count=len(sessions),
            active_count=len(active),
            default_agent=effective_default,
            runtime_version=version,
            runtime_available=runtime_available,
            orphan_terminals=orphans,
        )

    def rebind(self, workspace: Workspace, new_root: str) -> Workspace:
        """Explicitly move a workspace identity to a new canonical root."""
        other = self.store.workspaces.find_by_root(new_root)
        if other is not None and other.id != workspace.id:
            raise UsageError(
                f"{new_root} is already bound to workspace {other.id}",
                "workspaces cannot share a root",
            )

        def relocate(path: str) -> str:
            if not is_inside(path, workspace.root_path):
                return path
            return str(Path(new_root) / Path(path).relative_to(workspace.root_path))

        updates = []
        for session in self.store.sessions.list_for_workspace(workspace.id, include_deleted=True):
            cwd = relocate(session.cwd)
            worktree_path = relocate(session.worktree_path) if session.worktree_path is not None else None
            git_root = relocate(session.git_root) if session.git_root is not None else None
            native_session_dir = (
                relocate(session.native_session_dir) if session.native_session_dir is not None else None
            )
            updates.append((session, cwd, worktree_path, git_root, native_session_dir))

        for session, _cwd, worktree_path, git_root, _native in updates:
            if (
                worktree_path is not None
                and git_root is not None
                and Path(worktree_path).is_dir()
                and (worktree_path != session.worktree_path or git_root != session.git_root)
            ):
                repair_worktree(git_root, worktree_path)

        stamp = now()
        with transaction(self.store.conn):
            other = self.store.workspaces.find_by_root(new_root)
            if other is not None and other.id != workspace.id:
                raise UsageError(
                    f"{new_root} is already bound to workspace {other.id}",
                    "workspaces cannot share a root",
                )
            for session, cwd, worktree_path, git_root, native_session_dir in updates:
                self.store.sessions.update_paths(
                    session.id,
                    cwd=cwd,
                    worktree_path=worktree_path,
                    git_root=git_root,
                    native_session_dir=native_session_dir,
                    updated_at=stamp,
                )
            self.store.workspaces.update_root(workspace.id, new_root, stamp)
        workspace.root_path = new_root
        workspace.updated_at = stamp
        return workspace
