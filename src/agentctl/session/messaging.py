"""Session messaging: send text, read output, wait for a turn."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from agentctl.errors import RuntimeFailure
from agentctl.runtime.base import ReadOutput, RuntimeAdapter, WaitOutcome
from agentctl.session.lifecycle import LifecycleManager
from agentctl.storage.models import Session


@dataclass(slots=True)
class SendReport:
    session: str
    terminal: str
    started: bool | None
    status: str | None
    turn: int

    def to_dict(self) -> dict[str, object]:
        return {
            "session": self.session,
            "terminal": self.terminal,
            "started": self.started,
            "status": self.status,
            "turn": self.turn,
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

    def send(self, session: Session, text: str, *, ready_timeout: float = 30.0, force: bool = False) -> SendReport:
        address = self._terminal_address(session)
        # A send to a booting agent can be dropped; wait for its first report.
        try:
            self.lifecycle.wait_until_ready(session, timeout=ready_timeout)
        except RuntimeFailure:
            raise
        result = self.runtime.send_input(address, text, force=force)
        return SendReport(
            session=session.name,
            terminal=address,
            started=result.get("started"),
            status=result.get("status"),
            turn=int(result.get("turn") or 0),
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
