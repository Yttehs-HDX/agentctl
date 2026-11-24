"""Session messaging: send text, read output, wait for a turn."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from agentctl.agents.definitions import AGENT_DEFINITIONS
from agentctl.errors import DeliveryUnconfirmed, RuntimeFailure, UsageError
from agentctl.runtime.base import ReadOutput, RuntimeAdapter, WaitOutcome
from agentctl.session.lifecycle import LifecycleManager
from agentctl.storage.models import Session

DEFAULT_READY_TIMEOUT = 15.0


@dataclass(slots=True)
class SendReport:
    session: str
    terminal: str
    started: bool | None
    status: str | None
    turn: int
    confirmed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "session": self.session,
            "terminal": self.terminal,
            "started": self.started,
            "status": self.status,
            "turn": self.turn,
            "confirmed": self.confirmed,
        }


@dataclass(slots=True)
class KeyReport:
    session: str
    terminal: str
    status: str | None
    turn: int

    def to_dict(self) -> dict[str, object]:
        return {
            "session": self.session,
            "terminal": self.terminal,
            "status": self.status,
            "turn": self.turn,
        }


@dataclass(slots=True)
class InterruptReport:
    session: str
    terminal: str
    key: str | None
    outcome: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "session": self.session,
            "terminal": self.terminal,
            "key": self.key,
            "outcome": self.outcome,
        }


class MessagingService:
    def __init__(self, runtime: RuntimeAdapter, lifecycle: LifecycleManager) -> None:
        self.runtime = runtime
        self.lifecycle = lifecycle

    def _terminal_address(self, session: Session, *, require_live: bool = True) -> str:
        terminal = self.lifecycle.find_terminal(session)
        if terminal is None:
            raise RuntimeFailure(
                f"session {session.name!r} has no runtime terminal",
                "run `agentctl recover` to re-discover it, or delete and recreate the session",
            )
        if require_live and not terminal.live:
            raise RuntimeFailure(
                f"session {session.name!r} has finished ({terminal.reason or 'exited'})",
                "delete it and create a new session",
            )
        return terminal.address

    def send(
        self,
        session: Session,
        text: str,
        *,
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
        force: bool = False,
    ) -> SendReport:
        address = self._terminal_address(session)
        definition = AGENT_DEFINITIONS.get(session.agent)
        integration = bool(definition and definition.integration)
        confirmed = False
        if not force:
            if not integration:
                raise DeliveryUnconfirmed(
                    f"agent {session.agent!r} does not report status, so delivery cannot be confirmed",
                    "pass --force to type anyway, or use an agent that reports status",
                )
            # A send to a booting agent can be dropped; wait for its first report.
            self.lifecycle.wait_until_ready(session, timeout=ready_timeout)
            terminal = self.lifecycle.find_terminal(session)
            if terminal is None or not terminal.live:
                raise RuntimeFailure(
                    f"session {session.name!r} stopped before it could accept input",
                    "run `agentctl status`, then create a new session",
                )
            address = terminal.address
            if not terminal.reported:
                raise DeliveryUnconfirmed(
                    f"session {session.name!r} reported no status within {ready_timeout:g}s",
                    f"run `agentctl read {session.name}`, answer any dialog with "
                    f"`agentctl keys {session.name} ...`, then retry; `--force` types anyway "
                    "and may submit the dialog's selected option",
                )
            confirmed = True
        result = self.runtime.send_input(address, text, force=force)
        return SendReport(
            session=session.name,
            terminal=address,
            started=result.get("started"),
            status=result.get("status"),
            turn=int(result.get("turn") or 0),
            confirmed=confirmed,
        )

    def keys(self, session: Session, keys: list[str]) -> KeyReport:
        if not keys:
            raise UsageError("at least one key name is required")
        for key in keys:
            if key.startswith("-"):
                raise UsageError(f"invalid key name {key!r}: key names never start with '-'")
        address = self._terminal_address(session)
        body = self.runtime.send_keys(address, keys)
        return KeyReport(
            session=session.name,
            terminal=address,
            status=body.get("status"),
            turn=int(body.get("turn") or 0),
        )

    def interrupt(self, session: Session, *, timeout: float | None = None) -> InterruptReport:
        address = self._terminal_address(session)
        body = self.runtime.interrupt(address, timeout=timeout)
        return InterruptReport(
            session=session.name,
            terminal=address,
            key=body.get("key"),
            outcome=body.get("outcome"),
        )

    def read(self, session: Session, *, lines: int = 50) -> ReadOutput:
        # Output of a finished terminal is still readable; only sending needs it live.
        address = self._terminal_address(session, require_live=False)
        return self.runtime.read_output(address, lines=lines)

    def follow(
        self,
        session: Session,
        *,
        lines: int = 50,
        interval: float = 1.0,
        stream=sys.stdout,
    ) -> None:
        """Print screen snapshots as they change until the session stops being live."""
        last: str | None = None
        try:
            while True:
                output = self.runtime.read_output(self._terminal_address(session), lines=lines)
                if output.text != last:
                    if last is not None:
                        print("", file=stream)
                    print(output.text, file=stream, flush=True)
                    last = output.text
                terminal = self.lifecycle.find_terminal(session)
                if terminal is None or not terminal.live:
                    print("", file=stream)
                    print(f"[agentctl] session {session.name} is no longer live", file=stream)
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            return

    def wait(self, session: Session, *, timeout: float | None = None) -> WaitOutcome:
        address = self._terminal_address(session)
        return self.runtime.wait(address, timeout=timeout)
