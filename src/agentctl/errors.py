"""Error types shared by every layer.

Each error carries a stable machine code, an operator-facing hint and the
process exit status the CLI must return.
"""

from __future__ import annotations

from typing import Any

from agentctl import JSON_SCHEMA

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_NOT_READY = 4  # setup / init missing
EXIT_CONFLICT = 5
EXIT_INVALID = 6
EXIT_RUNTIME = 7
EXIT_BLOCKED = 8  # needs a human decision


class AgentctlError(Exception):
    """Base error: message, hint, machine-readable code and exit status."""

    code = "error"
    exit_code = EXIT_ERROR

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": type(self).__name__,
            "code": self.code,
            "message": self.message,
        }
        if self.hint:
            body["hint"] = self.hint
        return {"schema": JSON_SCHEMA, "error": body}


class UsageError(AgentctlError):
    code = "usage"
    exit_code = EXIT_USAGE


class SetupRequired(AgentctlError):
    code = "setup_required"
    exit_code = EXIT_NOT_READY


class WorkspaceNotInitialized(AgentctlError):
    code = "workspace_not_initialized"
    exit_code = EXIT_NOT_READY


class ConfigError(AgentctlError):
    code = "config_error"
    exit_code = EXIT_ERROR


class InvalidPath(AgentctlError):
    code = "invalid_path"
    exit_code = EXIT_INVALID


class AgentUnknown(AgentctlError):
    code = "agent_unknown"
    exit_code = EXIT_INVALID


class AgentDisabled(AgentctlError):
    code = "agent_disabled"
    exit_code = EXIT_INVALID


class AgentNotInstalled(AgentctlError):
    code = "agent_not_installed"
    exit_code = EXIT_INVALID


class AgentNotConfigured(AgentctlError):
    code = "agent_not_configured"
    exit_code = EXIT_INVALID


class SessionNotFound(AgentctlError):
    code = "session_not_found"
    exit_code = EXIT_NOT_FOUND


class SessionNameConflict(AgentctlError):
    code = "session_name_conflict"
    exit_code = EXIT_CONFLICT


class RuntimeUnavailable(AgentctlError):
    code = "runtime_unavailable"
    exit_code = EXIT_RUNTIME


class RuntimeFailure(AgentctlError):
    """An HC call failed. `kind` keeps the runtime's own error word."""

    code = "runtime_error"
    exit_code = EXIT_RUNTIME

    def __init__(
        self,
        message: str,
        hint: str | None = None,
        *,
        kind: str | None = None,
        kind_hint: str | None = None,
    ) -> None:
        super().__init__(message, hint or kind_hint)
        self.kind = kind

    def to_dict(self) -> dict[str, Any]:
        body = super().to_dict()
        if self.kind:
            body["error"]["runtime_kind"] = self.kind
        return body


class RuntimeNotFound(RuntimeFailure):
    code = "runtime_not_found"
    exit_code = EXIT_NOT_FOUND


class RuntimeSpawnFailed(RuntimeFailure):
    code = "runtime_spawn_failed"


class RuntimeBlocked(RuntimeFailure):
    code = "runtime_blocked"
    exit_code = EXIT_BLOCKED


class DeliveryUnconfirmed(AgentctlError):
    code = "delivery_unconfirmed"
    exit_code = EXIT_BLOCKED  # 8, a human decision is needed


class RuntimeFinished(RuntimeFailure):
    code = "runtime_finished"
