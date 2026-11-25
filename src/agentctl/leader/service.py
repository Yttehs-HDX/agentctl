"""Leader binding: which terminal, if any, is the workspace's Leader?

What this is
------------
A record of where the Leader is running, so a fresh Leader can find the pool it
left behind. Nothing more. The row says "workspace ws_x is led from terminal t7,
native session 01a2…". It grants no authority and blocks no other process.

Why there is no lease, epoch or token
-------------------------------------
v0.2 tried to make "one live Leader" a hard guarantee with expiring leases and
fencing tokens. That was agentctl re-implementing a supervisor, and it broke the
first principle: Herdr already owns terminal liveness. A terminal's `state` is
the truth about whether a Leader is alive, and Herdr reports it.

So the rule is the simple one:

- two Leaders are prevented by *using* the same binding, not by a lock;
- a stale binding is detected by asking Herdr, not by a timeout;
- `agentctl leader attach` writes the binding; `leader status` compares it with
  what Herdr says about that terminal.

The one thing still enforced is that a Worker cannot silently become the
Leader: a Worker is launched with the `agentctl.role=worker` label and `attach`
refuses a terminal carrying it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from agentctl.clock import now
from agentctl.errors import LeaderRequired, UsageError
from agentctl.runtime.base import RuntimeAdapter, TerminalInfo
from agentctl.storage.database import transaction
from agentctl.storage.models import LeaderBinding, Workspace

#: Label marking a terminal as a Worker, so it can never be adopted as Leader.
ROLE_LABEL = "agentctl.role"
WORKER_ROLE = "worker"
LEADER_ROLE = "leader"
#: Environment variable a Leader's terminal can carry, for harnesses that start
#: outside Herdr and want the same binding without a flag.
LEADER_ENV = "AGENTCTL_LEADER"


@dataclass(slots=True)
class LeaderStatus:
    workspace: Workspace
    binding: LeaderBinding | None
    terminal: TerminalInfo | None
    #: True when the binding's terminal is still live according to Herdr.
    live: bool
    #: The terminal a new Leader should attach to (the binding's, when live).
    reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace.id,
            "leader_id": self.workspace.leader_id,
            "bound": self.binding is not None,
            "live": self.live,
            "reason": self.reason,
            "binding": self.binding.to_dict() if self.binding else None,
            "terminal": {
                "address": self.terminal.address,
                "state": self.terminal.state,
                "status": self.terminal.status,
            }
            if self.terminal
            else None,
        }


class LeaderService:
    """Read and write the workspace's Leader binding.

    Deliberately small: no locks, no expiry, no token. Every method either reads
    or writes one row, and liveness always comes from Herdr.
    """

    def __init__(self, store, runtime: RuntimeAdapter) -> None:
        self.store = store
        self.runtime = runtime

    # ------------------------------------------------------------------ read

    def current(self, workspace: Workspace) -> LeaderBinding | None:
        return self.store.leaders.get(workspace.id)

    def _terminal_for(self, binding: LeaderBinding | None) -> TerminalInfo | None:
        if binding is None or binding.terminal is None:
            return None
        terminal = self.runtime.find_terminal(binding.terminal)
        if (
            terminal is not None
            and terminal.is_local
            and terminal.labels.get("agentctl.workspace") == binding.workspace_id
            and terminal.labels.get(ROLE_LABEL) == LEADER_ROLE
        ):
            return terminal
        return None

    def _live_bound_terminal(self, binding: LeaderBinding | None) -> TerminalInfo | None:
        """The bound terminal, only when Herdr reports it live.

        A finished terminal frees the binding: the address may still resolve,
        but nothing is running there, so any Leader may attach.
        """
        terminal = self._terminal_for(binding)
        if terminal is None or not terminal.live:
            return None
        return terminal

    def status(self, workspace: Workspace) -> LeaderStatus:
        binding = self.current(workspace)
        if binding is None:
            return LeaderStatus(workspace, None, None, False, "no Leader has attached yet")
        terminal = self._terminal_for(binding)
        if terminal is None:
            return LeaderStatus(
                workspace,
                binding,
                None,
                False,
                "the bound terminal is gone; any Leader may attach",
            )
        if not terminal.live:
            return LeaderStatus(
                workspace, binding, terminal, False, "the bound terminal has finished"
            )
        return LeaderStatus(workspace, binding, terminal, True, None)

    # ----------------------------------------------------------------- write

    def attach(
        self,
        workspace: Workspace,
        *,
        terminal: str | None = None,
        native_session_id: str | None = None,
        take_over: bool = False,
    ) -> LeaderBinding:
        """Bind this Leader to a terminal (or to nothing, for an outside Leader).

        `terminal` defaults to `$HC_TERMINAL_ID`, which is how a Leader running
        inside Herdr identifies itself without arguments. An outside Leader
        passes no terminal and gets a binding with no address; Herdr then reports
        it as not live, which is accurate.

        The staleness decision, the row write and the role label all happen
        inside one `BEGIN IMMEDIATE` transaction. That is what keeps a binding
        from ever being visible without its label: a competitor that read the
        row as stale must first wait for the writer to commit, and then sees the
        label and refuses. The cost is that the `hc` calls run under the write
        lock, for one short attach.
        """
        address = terminal or os.environ.get("HC_TERMINAL_ID") or None
        terminal_info: TerminalInfo | None = None
        if address:
            terminal_info = self.runtime.find_terminal(address)
            if terminal_info is None or not terminal_info.is_local or not terminal_info.live:
                raise UsageError(
                    f"terminal {address} is not a live local terminal",
                    "run this from inside the Leader's Herdr terminal, or pass --terminal",
                )
            if (
                terminal_info.labels.get(ROLE_LABEL) == WORKER_ROLE
                or "agentctl.session" in terminal_info.labels
            ):
                raise LeaderRequired(
                    f"terminal {address} is a Worker, not a Leader",
                    "a Worker never becomes the Leader implicitly; run `agentctl leader attach` "
                    "from the Leader's own terminal",
                )
            address = terminal_info.address
            owner = terminal_info.labels.get("agentctl.workspace")
            if owner and owner != workspace.id:
                raise UsageError(
                    f"terminal {address} belongs to workspace {owner}",
                    "attach from a terminal in this workspace, or pass --terminal",
                )

        stamp = now()
        with transaction(self.store.conn):
            existing = self.store.leaders.get(workspace.id)
            if existing is not None and not take_over:
                bound = self._live_bound_terminal(existing)
                if bound is not None and bound.address != address:
                    raise LeaderRequired(
                        f"workspace {workspace.id} is already led from terminal "
                        f"{existing.terminal} ({existing.native_session_id or 'no session id'})",
                        "reuse that terminal, or run `agentctl leader attach --takeover` "
                        "if it is really gone",
                    )
            binding = LeaderBinding(
                workspace_id=workspace.id,
                leader_id=workspace.leader_id,
                terminal=address,
                native_session_id=native_session_id or existing_native(existing),
                attached_at=stamp,
                updated_at=stamp,
            )
            if existing is None:
                won = self.store.leaders.insert_if_absent(binding)
            else:
                won = self.store.leaders.update_if_unchanged(
                    binding,
                    expected_terminal=existing.terminal,
                    expected_native=existing.native_session_id,
                )
            if not won:
                raise LeaderRequired(
                    "another Leader attached while this one was validating",
                    "check `agentctl leader status`; attach again only if that terminal is gone",
                )
            if terminal_info is not None:
                # Inside the transaction: a loser must not claim the terminal,
                # and a winner must never be visible before its label.
                self.runtime.label(
                    terminal_info.address,
                    {"agentctl.workspace": workspace.id, ROLE_LABEL: LEADER_ROLE},
                )
        self.store.workspaces.touch(workspace.id, stamp)
        return binding

    def detach(self, workspace: Workspace) -> bool:
        """Forget the binding. The terminal keeps running; only the record goes."""
        with self.store.conn:
            removed = self.store.leaders.delete(workspace.id)
        self.store.workspaces.touch(workspace.id, now())
        return removed


def existing_native(binding: LeaderBinding | None) -> str | None:
    return binding.native_session_id if binding else None



__all__ = [
    "LEADER_ENV",
    "LEADER_ROLE",
    "ROLE_LABEL",
    "WORKER_ROLE",
    "LeaderService",
    "LeaderStatus",
]
