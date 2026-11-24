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


# ---------------------------------------------------------------------------
# v0.2 records
#
# The state words below are the contract the rest of the code branches on. Each
# transition that has an observable meaning to a human is named exactly once
# here, so no service invents its own vocabulary.
# ---------------------------------------------------------------------------

# A message moves forward only through these words. `delivery_unknown` is not a
# failure state: it means the transport may have accepted the text, so the
# message must never be blind-resubmitted.
MessageState = Literal[
    "queued",
    "dispatching",
    "dispatched",
    "acknowledged",
    "running",
    "completed",
    "failed",
    "cancelled",
    "delivery_unknown",
]

MESSAGE_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})

InboxState = Literal["pending", "claimed", "acked", "skipped"]

EventSeverity = Literal["info", "warning", "error"]


@dataclass(slots=True)
class LeaderLease:
    workspace_id: str
    leader_id: str
    leader_instance_id: str
    leader_epoch: int
    lease_token: str
    acquired_at: str
    renewed_at: str
    expires_at: str
    released_at: str | None

    @property
    def held(self) -> bool:
        return self.released_at is None

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "leader_id": self.leader_id,
            "leader_instance_id": self.leader_instance_id,
            "leader_epoch": self.leader_epoch,
            "lease_token": self.lease_token,
            "acquired_at": self.acquired_at,
            "renewed_at": self.renewed_at,
            "expires_at": self.expires_at,
            "released_at": self.released_at,
        }


@dataclass(slots=True)
class Message:
    id: str
    workspace_id: str
    session_id: str
    sender_id: str
    recipient_id: str
    payload: str
    state: str
    attempt_count: int
    created_at: str
    dispatched_at: str | None
    acknowledged_at: str | None
    completed_at: str | None
    last_error: str | None
    ack_token: str | None

    @property
    def terminal(self) -> bool:
        return self.state in MESSAGE_TERMINAL_STATES

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "sender_id": self.sender_id,
            "recipient_id": self.recipient_id,
            "payload": self.payload,
            "state": self.state,
            "attempt_count": self.attempt_count,
            "created_at": self.created_at,
            "dispatched_at": self.dispatched_at,
            "acknowledged_at": self.acknowledged_at,
            "completed_at": self.completed_at,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class Event:
    id: str
    workspace_id: str
    event: str
    session_id: str | None
    message_id: str | None
    leader_epoch: int | None
    source: str | None
    severity: str
    payload: dict[str, object]
    dedupe_key: str | None
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event_id": self.id,
            "event": self.event,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "leader_epoch": self.leader_epoch,
            "source": self.source,
            "severity": self.severity,
            "timestamp": self.created_at,
            "payload": self.payload,
            "dedupe_key": self.dedupe_key,
        }


@dataclass(slots=True)
class InboxItem:
    id: str
    event_id: str
    workspace_id: str
    leader_id: str
    state: str
    created_at: str
    claimed_at: str | None
    claimed_by: str | None
    acked_at: str | None
    event: Event | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "workspace_id": self.workspace_id,
            "leader_id": self.leader_id,
            "state": self.state,
            "created_at": self.created_at,
            "claimed_at": self.claimed_at,
            "claimed_by": self.claimed_by,
            "acked_at": self.acked_at,
            "event": self.event.to_dict() if self.event else None,
        }


@dataclass(slots=True)
class OutboxItem:
    id: str
    workspace_id: str
    kind: str
    subject_id: str
    state: str
    attempt_count: int
    not_before: str
    created_at: str
    last_error: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "state": self.state,
            "attempt_count": self.attempt_count,
            "not_before": self.not_before,
            "created_at": self.created_at,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class Integration:
    workspace_id: str
    agent: str
    scope: str
    hook_path: str | None
    settings_path: str | None
    capability: str
    version: str | None
    installed_at: str
    updated_at: str
    config_dir: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "agent": self.agent,
            "scope": self.scope,
            "hook_path": self.hook_path,
            "settings_path": self.settings_path,
            "config_dir": self.config_dir,
            "capability": self.capability,
            "version": self.version,
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
        }
