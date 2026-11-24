"""Runtime abstraction: what agentctl needs from a terminal substrate.

The adapter is the only place that knows how a terminal is created, talked to
and inspected. Business code holds `RuntimeAdapter` and opaque terminal ids.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TerminalInfo:
    address: str
    name: str | None
    state: str  # live | finished
    status: str | None  # idle | working | done | blocked | error | None
    app: str | None
    cwd: str
    exit_code: int | None
    reason: str | None
    labels: dict[str, str] = field(default_factory=dict)
    reported: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def live(self) -> bool:
        return self.state == "live"

    @property
    def is_local(self) -> bool:
        """False for `hc`'s remote form `machine/t7`.

        agentctl only ever creates local terminals, so a remote one carrying
        our labels is somebody else's work: adopting it would type into another
        machine and let `delete` kill a terminal we do not own.
        """
        return "/" not in self.address


@dataclass(slots=True)
class ReadOutput:
    text: str
    complete: bool
    how: str
    why: str | None


@dataclass(slots=True)
class WaitOutcome:
    ended: bool
    reason: str
    status: str | None
    exit_code: int | None
    turn: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def turn_ended(self) -> bool:
        """A worker's turn finished, whether it reported or merely stopped.

        Herdr returns `reason: "status"` when a child left `working`. That is the
        agent-agnostic completion signal: it does not depend on the child calling
        `hc send`, and it does not require parsing the screen.
        """
        return self.ended and self.reason == "status"

    @property
    def timed_out(self) -> bool:
        return self.reason == "timeout"


class RuntimeAdapter(ABC):
    """Terminal lifecycle for one runtime backend."""

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def version(self) -> str: ...

    @abstractmethod
    def create_terminal(
        self,
        argv: list[str],
        *,
        cwd: str,
        name: str,
        labels: dict[str, str] | None = None,
        parent: str | None = None,
        outlive_parent: bool = False,
        first_task: str | None = None,
    ) -> TerminalInfo: ...

    @abstractmethod
    def send_input(self, terminal: str, text: str) -> dict[str, Any]: ...

    @abstractmethod
    def send_keys(self, terminal: str, keys: list[str]) -> dict[str, Any]: ...

    @abstractmethod
    def interrupt(self, terminal: str, *, timeout: float | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def read_output(self, terminal: str, *, lines: int = 0) -> ReadOutput: ...

    @abstractmethod
    def get_terminal(self, terminal: str) -> TerminalInfo: ...

    @abstractmethod
    def find_terminal(self, terminal: str) -> TerminalInfo | None: ...

    @abstractmethod
    def list_terminals(
        self, *, labels: dict[str, str] | None = None, include_finished: bool = False
    ) -> list[TerminalInfo]: ...

    @abstractmethod
    def wait(self, terminal: str, *, timeout: float | None = None) -> WaitOutcome: ...

    @abstractmethod
    def label(self, terminal: str, labels: dict[str, str]) -> TerminalInfo: ...

    @abstractmethod
    def wait_children(
        self, *, parent: str | None = None, mode: str = "any", timeout: float | None = None
    ) -> WaitOutcome: ...

    @abstractmethod
    def kill(self, terminal: str) -> TerminalInfo: ...

    @abstractmethod
    def remove(self, terminal: str) -> None: ...
