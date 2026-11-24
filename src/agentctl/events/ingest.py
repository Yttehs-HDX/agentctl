"""Turn Herdr Core fact changes into agentctl events.

Herdr Core streams *terminal* facts (`status`, `turn`, `finished`, ...). What a
Leader cares about is *work* facts: a turn finished, a worker needs a person, a
session died. This module is the only translation between the two, and it is
pure so every rule can be tested without a live core.

Exactly one rule is subtle. Herdr Core reports `status: done` and a separate
`turn` bump; either may arrive first, and a resumed core replays a snapshot.
Emission is therefore keyed on `(session, turn)` so a replayed or reordered pair
produces one `turn.completed`, never two.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentctl.clock import now
from agentctl.events.bus import EventBus
from agentctl.storage.models import Session, Workspace


@dataclass(slots=True)
class TerminalView:
    """The facts about one terminal that matter to the mapper."""

    address: str
    state: str = "live"
    status: str | None = None
    reported: bool = False
    turn: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    exit_code: int | None = None
    reason: str | None = None

    @classmethod
    def parse(cls, body: dict) -> "TerminalView":
        status = body.get("status")
        word = status.get("state") if isinstance(status, dict) else status
        return cls(
            address=body.get("address", ""),
            state=body.get("state", "live"),
            status=word,
            reported=bool(body.get("reported", False)),
            turn=int(body.get("turn") or 0),
            labels=dict(body.get("labels") or {}),
            exit_code=body.get("exit_code"),
            reason=body.get("reason"),
        )


class Ingestor:
    """Stateful mapper: one instance per daemon process.

    The state it keeps is the minimum needed to distinguish a transition from a
    replay: the last status word and turn per terminal, and whether a terminal's
    exit was already reported.
    """

    def __init__(self, store, bus: EventBus) -> None:
        self.store = store
        self.bus = bus
        self._last_status: dict[str, str | None] = {}
        self._done_turns: set[tuple[str, int]] = set()
        self._exited: set[str] = set()
        self._blocked: dict[str, bool] = {}
        self._block_counts: dict[tuple[str, int], int] = {}

    # ------------------------------------------------------------------ input

    def observe(self, workspace: Workspace, view: TerminalView) -> list[str]:
        """Apply one terminal fact. Returns the event names emitted."""
        session = self._session_for(workspace, view)
        if session is None:
            return []
        emitted: list[str] = []

        if view.state == "finished":
            if view.address not in self._exited:
                self._exited.add(view.address)
                self._emit(
                    workspace,
                    "session.exited",
                    session,
                    payload={
                        "reason": view.reason,
                        "exit_code": view.exit_code,
                        "status": view.status,
                    },
                    dedupe=f"{session.id}:exited:{view.reason}:{view.exit_code}",
                )
                emitted.append("session.exited")
            return emitted

        previous = self._last_status.get(view.address)
        self._last_status[view.address] = view.status

        if view.reported and view.status == "done":
            key = (session.id, view.turn)
            if key not in self._done_turns:
                self._done_turns.add(key)
                self._emit(
                    workspace,
                    "turn.completed",
                    session,
                    payload={"turn": view.turn, "source": "herdr-core", "status": view.status},
                    dedupe=f"{session.id}:turn:{view.turn}:completed",
                )
                emitted.append("turn.completed")

        if view.reported and view.status != "blocked":
            # Leaving `blocked` re-arms reporting, so the next prompt is news.
            self._blocked[view.address] = False

        if view.reported and view.status == "blocked":
            # A worker can block several times inside one turn (a run of
            # permission prompts), so the key counts occurrences within the turn
            # rather than assuming one. A replayed identical observation is
            # absorbed by the count staying the same.
            was_blocked = self._blocked.get(view.address, False)
            self._blocked[view.address] = True
            if not was_blocked:
                count = self._block_counts.get((session.id, view.turn), 0) + 1
                self._block_counts[(session.id, view.turn)] = count
                self._emit(
                    workspace,
                    "worker.blocked",
                    session,
                    payload={"turn": view.turn, "occurrence": count},
                    dedupe=f"{session.id}:turn:{view.turn}:blocked:{count}",
                )
                emitted.append("worker.blocked")

        if previous is not None and previous != view.status:
            self._emit(
                workspace,
                "session.status_changed",
                session,
                payload={"from": previous, "to": view.status, "turn": view.turn},
                dedupe=None,
            )
            emitted.append("session.status_changed")
        return emitted

    # ----------------------------------------------------------------- output

    def _emit(self, workspace: Workspace, event: str, session: Session, *, payload, dedupe: str | None) -> None:
        self.bus.emit(
            workspace,
            event,
            session_id=session.id,
            source="herdr-core",
            payload={"session": session.name, **payload},
            dedupe_key=dedupe,
        )

    def _session_for(self, workspace: Workspace, view: TerminalView) -> Session | None:
        """Map a terminal to a session, refusing cross-workspace adoption.

        Labels are the durable link; the persisted address is the fast path. A
        terminal carrying another workspace's label is never adopted, even if
        it sits on the same core.
        """
        labelled_workspace = view.labels.get("agentctl.workspace")
        labelled_session = view.labels.get("agentctl.session")
        if labelled_workspace and labelled_workspace != workspace.id:
            return None
        if labelled_session:
            session = self.store.sessions.find_by_id(labelled_session)
            if session is not None and session.workspace_id == workspace.id and session.alive:
                return session
        for candidate in self.store.sessions.list_for_workspace(workspace.id):
            if candidate.hc_terminal_id == view.address:
                return candidate
        return None


@dataclass(slots=True)
class Snapshot:
    """A `hc events` snapshot: the current facts, not a change."""

    terminals: list[TerminalView]
    seq: int
    captured_at: str = field(default_factory=now)
