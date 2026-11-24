"""Domain records shared by services, storage and the CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SessionMode = Literal["shared", "worktree"]

# Lifecycle states stored in the registry. `unknown` and `unreachable` mean the
# runtime could not tell us; they are never guessed.
SessionStatus = Literal[
    "starting",
    "idle",
    "working",
    "waiting",
    "completed",
    "failed",
    "stopped",
    "unknown",
    "unreachable",
]


@dataclass(slots=True)
class Workspace:
    id: str
    root_path: str
    leader_id: str
    default_agent: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "root": self.root_path,
            "leader_id": self.leader_id,
            "default_agent": self.default_agent,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class Session:
    id: str
    workspace_id: str
    name: str
    agent: str
    cwd: str
    mode: str
    hc_terminal_id: str | None
    hc_name: str | None
    native_session_id: str | None
    native_session_dir: str | None
    worktree_path: str | None
    worktree_branch: str | None
    git_root: str | None
    runtime_started_at: str | None
    status: str
    last_error: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None

    @property
    def alive(self) -> bool:
        return self.deleted_at is None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "agent": self.agent,
            "cwd": self.cwd,
            "mode": self.mode,
            "hc_terminal_id": self.hc_terminal_id,
            "hc_name": self.hc_name,
            "native_session_id": self.native_session_id,
            "native_session_dir": self.native_session_dir,
            "worktree_path": self.worktree_path,
            "worktree_branch": self.worktree_branch,
            "git_root": self.git_root,
            "runtime_started_at": self.runtime_started_at,
            "status": self.status,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deleted_at": self.deleted_at,
        }


@dataclass(slots=True)
class AgentSpec:
    """One catalog entry plus what the local machine actually supports."""

    id: str
    command: list[str]
    enabled: bool
    supports_native_session: bool
    supports_resume: bool
    integration: bool
    notes: str = ""
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class LeaderBinding:
    """Where the workspace's Leader runs. A pointer, not a lock.

    `terminal` is a Herdr address (`t7`), empty for a Leader outside Herdr.
    Liveness is never stored: `LeaderService` asks Herdr each time.
    """

    workspace_id: str
    leader_id: str
    terminal: str | None
    native_session_id: str | None
    attached_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "leader_id": self.leader_id,
            "terminal": self.terminal,
            "native_session_id": self.native_session_id,
            "attached_at": self.attached_at,
            "updated_at": self.updated_at,
        }
