"""Session lifecycle: status derivation, readiness and registry reconciliation.

The registry is the source of truth for *identity*; the runtime is the source
of truth for *liveness*. Nothing is guessed: when the agent does not report a
status word, the session is `unknown`.
"""

from __future__ import annotations

import time

from agentctl.errors import RuntimeFailure
from agentctl.runtime.base import RuntimeAdapter, TerminalInfo
from agentctl.storage.models import Session
from agentctl.workspace.resolver import now

STARTING_GRACE_SECONDS = 90.0


def derive_status(
    session: Session,
    terminal: TerminalInfo | None,
    *,
    integration: bool,
    created_recently: bool,
) -> tuple[str, str | None]:
    """Map runtime facts to an agentctl status plus an explanation."""
    if terminal is None:
        return "unknown", "runtime terminal not found"
    if not terminal.live:
        if terminal.reason == "killed":
            return "stopped", "terminated"
        if terminal.exit_code not in (None, 0):
            return "failed", f"agent exited with code {terminal.exit_code}"
        return "stopped", "agent exited"
    word = terminal.status
    if not integration or word is None:
        if created_recently:
            return "starting", None
        return "unknown", "agent does not report status"
    if word == "idle":
        return "idle", None
    if word == "working":
        return "working", None
    if word == "done":
        return "completed", None
    if word == "blocked":
        return "waiting", "agent needs input"
    if word == "error":
        return "failed", "agent reported an error"
    return "unknown", f"unrecognised status {word!r}"


def created_recently(session: Session, *, seconds: float = STARTING_GRACE_SECONDS) -> bool:
    """Whether the agent's process was launched within the startup grace.

    `runtime_started_at` moves on every (re)launch, so a resumed session gets
    the same grace period as a fresh one instead of looking broken at once.
    """
    from datetime import datetime

    stamp = session.runtime_started_at or session.created_at
    try:
        started = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    return (datetime.now(started.tzinfo) - started).total_seconds() < seconds


class LifecycleManager:
    def __init__(self, store, runtime: RuntimeAdapter) -> None:
        self.store = store
        self.runtime = runtime

    # ------------------------------------------------------------- discovery

    def find_terminal(self, session: Session) -> TerminalInfo | None:
        """Locate this session's runtime terminal: by persisted id, then by label.

        Only local terminals count. `hc` addresses remote ones as `machine/t7`,
        and `hc ls` returns this machine's terminals first, so a remote terminal
        that happens to carry our label must never be adopted: `send` would type
        into another machine and `delete` would kill work we never started.
        """
        if session.hc_terminal_id:
            terminal = self.runtime.find_terminal(session.hc_terminal_id)
            if terminal is not None and terminal.is_local:
                return terminal
        terminals = [
            t
            for t in self.runtime.list_terminals(
                labels={"agentctl.session": session.id}, include_finished=False
            )
            if t.is_local
        ]
        return terminals[0] if terminals else None

    def refresh(self, session: Session, *, persist: bool = True) -> Session:
        """Re-derive one session's status from the runtime and persist it."""
        from agentctl.agents.definitions import AGENT_DEFINITIONS
        from agentctl.agents.detector import installed

        definition = AGENT_DEFINITIONS.get(session.agent)
        integration = bool(definition and definition.integration and installed(definition))
        try:
            terminal = self.find_terminal(session)
        except RuntimeFailure as exc:
            if persist:
                self.store.sessions.set_status(session.id, "unknown", str(exc.message), now())
                session.status = "unknown"
                session.last_error = exc.message
            return session

        status, reason = derive_status(
            session,
            terminal,
            integration=integration,
            created_recently=created_recently(session),
        )
        if terminal is not None and (terminal.address != session.hc_terminal_id or terminal.name != session.hc_name):
            if persist:
                self.store.sessions.update_runtime(
                    session.id,
                    hc_terminal_id=terminal.address,
                    hc_name=terminal.name or session.hc_name,
                    native_session_id=session.native_session_id,
                    native_session_dir=session.native_session_dir,
                    status=status,
                    last_error=reason,
                    updated_at=now(),
                )
            session.hc_terminal_id = terminal.address
            session.hc_name = terminal.name or session.hc_name
        elif persist:
            self.store.sessions.set_status(session.id, status, reason, now())
        session.status = status
        session.last_error = reason
        return session

    def refresh_workspace(self, workspace_id: str) -> list[Session]:
        sessions = self.store.sessions.list_for_workspace(workspace_id)
        return [self.refresh(session) for session in sessions]

    # ------------------------------------------------------------- readiness

    def wait_until_ready(self, session: Session, *, timeout: float = 30.0) -> None:
        """Block until the agent has spoken (reported a status) or the timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            terminal = self.find_terminal(session)
            if terminal is None:
                raise RuntimeFailure(
                    f"terminal for session {session.name!r} disappeared",
                    "run `agentctl status`, then `agentctl recover`",
                )
            if not terminal.live or terminal.reported:
                return
            time.sleep(0.25)
