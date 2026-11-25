"""Session messaging: send text, read output, wait for a turn."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from agentctl.errors import RuntimeFailure, UsageError
from agentctl.runtime.base import ReadOutput, RuntimeAdapter, WaitOutcome
from agentctl.session.lifecycle import LifecycleManager
from agentctl.storage.models import Session

DEFAULT_READY_TIMEOUT = 15.0


@dataclass(slots=True)
class SendReport:
    """What Herdr reported about one send.

    `started` is Herdr's own answer to "did this start a turn": `False` means the
    text was accepted but the agent did not begin working (still booting, or a
    dialog is up), which is the honest form of a delivery doubt. There is no
    per-message acknowledgement to invent, and agentctl does not pretend to have
    one.
    """

    session: str
    terminal: str
    started: bool | None
    status: str | None
    turn: int
    wait: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "session": self.session,
            "terminal": self.terminal,
            "started": self.started,
            "status": self.status,
            "turn": self.turn,
            "wait": self.wait,
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
        force: bool = False,
        wait: bool = False,
        timeout: float | None = None,
    ) -> SendReport:
        """Type text into a session, letting Herdr own delivery and its guard.

        Herdr refuses a blocked agent and can wait for readiness; agentctl's only
        additions are identity (session → terminal) and passing `--wait` through,
        so `started: false` surfaces as a doubt instead of a silent success.
        """
        address = self._terminal_address(session)
        body = self.runtime.send_input(address, text, force=force, wait=wait, timeout=timeout)
        turn_result = body.get("wait") if isinstance(body.get("wait"), dict) else None
        return SendReport(
            session=session.name,
            terminal=address,
            started=body.get("started"),
            status=body.get("status"),
            turn=int(body.get("turn") or 0),
            wait=turn_result,
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
