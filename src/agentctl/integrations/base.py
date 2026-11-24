"""What an agentctl harness integration must provide, and what it may skip.

The interface is deliberately narrow. A hook is an untrusted, short-lived
process that runs inside somebody else's agent, so it may only report facts it
can actually observe. Where a harness cannot observe something, the adapter
says `False` in its capability record and the core degrades; it never fakes an
ACK, because a fake ACK would mark work delivered that the worker never saw.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What this harness can actually observe. Every field is measured."""

    agent: str
    version: str | None = None
    #: The harness exposes an ingress event carrying the submitted text, so an
    #: agentctl marker can be read out of it and an exact ACK is possible.
    receive_message: bool = False
    #: The harness can be told, while idle, to run a turn with given text.
    idle_wake: bool = False
    #: The harness reports when a turn begins.
    turn_started: bool = False
    #: The harness reports when a turn ends.
    turn_completed: bool = False
    #: The harness reports when a turn ends badly.
    turn_failed: bool = False
    #: Verified against a real binary on this machine, not just documented.
    verified: bool = False
    notes: str = ""
    missing: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "version": self.version,
            "receive_message": self.receive_message,
            "idle_wake": self.idle_wake,
            "turn_started": self.turn_started,
            "turn_completed": self.turn_completed,
            "turn_failed": self.turn_failed,
            "verified": self.verified,
            "notes": self.notes,
            "missing": list(self.missing),
        }


class HarnessAdapter:
    """One harness's hook contract.

    Subclasses implement `capabilities()` and `install()`, and provide the hook
    assets themselves. `notify_leader` defaults to "cannot", which is the honest
    answer for a harness without a wake path.
    """

    agent: str = ""
    #: Relative directories under the harness's user config root.
    supports_notify: bool = False

    def capabilities(self) -> Capabilities:  # pragma: no cover - abstract
        raise NotImplementedError

    def hook_sources(self) -> dict[str, str]:
        """name -> file content of the hook assets this adapter installs."""
        return {}

    def install(self, workspace, *, scope: str = "user") -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def uninstall(self, workspace, *, scope: str = "user") -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def notify_leader(self, event: dict, *, lease, session) -> bool:
        """Wake the Leader with one event. Returns whether it was delivered."""
        return False
