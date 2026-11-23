"""Workspace-level operations: init, info, listing."""

from __future__ import annotations

from dataclasses import dataclass

from agentctl.errors import UsageError
from agentctl.runtime.base import RuntimeAdapter
from agentctl.storage.models import Session, Workspace
from agentctl.workspace.resolver import WorkspaceResolver, now


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
            except Exception:  # noqa: BLE001 - info must not fail on a broken core
                version = None
        orphans: list[str] = []
        if runtime_available:
            try:
                terminals = self.runtime.list_terminals(labels={"agentctl.workspace": workspace.id})
            except Exception:  # noqa: BLE001 - same reasoning
                terminals = []
            known = {s.hc_terminal_id for s in sessions if s.hc_terminal_id}
            # Remote terminals (`machine/t7`) are never agentctl's: this
            # process only creates local ones, so listing them as orphans would
            # blame other machines' work on this workspace.
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
        stamp = now()
        self.store.workspaces.update_root(workspace.id, new_root, stamp)
        workspace.root_path = new_root
        workspace.updated_at = stamp
        return workspace
