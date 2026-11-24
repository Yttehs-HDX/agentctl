"""Message registry: queue, dispatch, and the honest bookkeeping between them.

The rule that shapes this module
--------------------------------
A message is **persisted before it is sent**, and its "I am about to send this"
state is committed before the text leaves. That ordering is what makes the
crash window visible: if the process dies after `dispatching` is written but
before the outcome is written, the message is left in `dispatching` and
`recover_stuck()` turns it into `delivery_unknown` rather than pretending it is
still queued. Such a message is never resubmitted automatically, because the
text may already have reached the worker.

Serial vs parallel
------------------
One worker runs one message at a time (enforced by the partial unique index),
so ordering within a session is real. Different sessions are independent: a
slow reviewer never delays a researcher.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentctl.agents.definitions import AGENT_DEFINITIONS
from agentctl.clock import new_id, now, token
from agentctl.errors import (
    EXIT_BLOCKED,
    AgentctlError,
    MessageBusy,
    MessageNotFound,
    MessageStateError,
    RuntimeFailure,
    UsageError,
)
from agentctl.events.bus import EventBus
from agentctl.messaging.transitions import can_move, describe
from agentctl.runtime.base import RuntimeAdapter
from agentctl.session.lifecycle import LifecycleManager
from agentctl.storage.database import transaction
from agentctl.storage.models import Message, Session, Workspace

# Marker carried inside the message text so a harness hook can name the exact
# message it received. Without this an ACK would only prove "some text arrived".
MARKER_OPEN = "[[agentctl:"
MARKER_CLOSE = "]]"


def marker_for(message_id: str) -> str:
    return f"{MARKER_OPEN}{message_id}{MARKER_CLOSE}"


def find_marker(text: str) -> str | None:
    """Extract a message id from arbitrary hook text, or None."""
    start = text.find(MARKER_OPEN)
    if start < 0:
        return None
    start += len(MARKER_OPEN)
    end = text.find(MARKER_CLOSE, start)
    if end < 0:
        return None
    candidate = text[start:end].strip()
    return candidate or None


def compose(payload: str, message_id: str) -> str:
    """The text actually typed into the worker terminal."""
    return f"{payload}\n\n[agentctl {marker_for(message_id)}]\nProcess this message, then stop and answer in this session."


@dataclass(slots=True)
class DispatchOutcome:
    message: Message
    confirmation: str  # "acknowledged" | "dispatched" | "delivery_unknown"
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "message": self.message.to_dict(),
            "confirmation": self.confirmation,
            "reason": self.reason,
        }


@dataclass(slots=True)
class AckResult:
    message: Message
    changed: bool
    event_created: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "message": self.message.to_dict(),
            "changed": self.changed,
            "event_created": self.event_created,
        }


class MessageService:
    def __init__(
        self,
        store,
        runtime: RuntimeAdapter,
        lifecycle: LifecycleManager,
        *,
        bus: EventBus | None = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.lifecycle = lifecycle
        self.bus = bus or EventBus(store)

    # ------------------------------------------------------------------ queue

    def enqueue(
        self,
        workspace: Workspace,
        session: Session,
        payload: str,
        *,
        sender_id: str | None = None,
        leader_epoch: int | None = None,
    ) -> Message:
        if not payload.strip():
            raise UsageError("refusing to queue an empty message")
        with transaction(self.store.conn):
            message = Message(
                id=new_id("msg"),
                workspace_id=workspace.id,
                session_id=session.id,
                sender_id=sender_id or workspace.leader_id,
                recipient_id=session.name,
                payload=payload,
                state="queued",
                attempt_count=0,
                created_at=now(),
                dispatched_at=None,
                acknowledged_at=None,
                completed_at=None,
                last_error=None,
                ack_token=token(16),
            )
            self.store.messages.insert(message)
            self.bus.emit(
                workspace,
                "message.queued",
                session_id=session.id,
                message_id=message.id,
                source="agentctl",
                leader_epoch=leader_epoch,
                payload={"session": session.name, "agent": session.agent},
                dedupe_key=f"{message.id}:queued",
            )
        return message

    # --------------------------------------------------------------- dispatch

    def dispatch(
        self, workspace: Workspace, message_id: str, *, allow_unconfirmed: bool = False
    ) -> DispatchOutcome:
        """Hand one queued message to the worker.

        The transport call happens outside the transaction that marks
        `dispatching`, deliberately: a long external call inside a write
        transaction would hold the database lock and stall every other writer.

        `allow_unconfirmed` is the explicit opt-in for an agent with no hook
        integration: the text is typed, and the recorded state is
        `delivery_unknown` instead of a fabricated `dispatched`.
        """
        message = self._require(workspace, message_id)
        if not can_move(message.state, "dispatching"):
            raise MessageStateError(
                f"message {message.id} is {message.state}; it cannot be dispatched again",
                f"only a queued message can be dispatched (this one is {describe(message.state)})",
            )
        # One worker runs one message at a time. A second message waits in the
        # queue instead of interleaving with the first, which is what makes
        # per-session ordering real rather than hopeful.
        active = self.store.messages.active_for_session(message.session_id)
        if active is not None and active.id != message.id:
            raise MessageBusy(
                f"session {message.recipient_id!r} is still working on message {active.id} ({active.state})",
                "the message stays queued; wait for `agentctl messages get "
                f"{active.id}` to complete, or cancel it",
            )
        session = self.store.sessions.find_by_id(message.session_id)
        if session is None or not session.alive:
            self._fail(workspace, message, "the target session no longer exists")
            raise MessageNotFound(
                f"message {message.id} targets a deleted session",
                "create the session again, or cancel the message",
            )

        with transaction(self.store.conn):
            moved = self.store.messages.update_state(
                message.id,
                "dispatching",
                expected=["queued"],
                increment_attempt=True,
                stamp=now(),
            )
            if not moved:
                raise MessageStateError(
                    f"message {message.id} changed state while dispatching",
                    "run `agentctl messages get` to see its current state",
                )

        # From here on the text may reach the worker, so every exit path records
        # an outcome; none leaves the row in `dispatching` on purpose.
        try:
            confirmed = self._deliver(message, session, allow_unconfirmed=allow_unconfirmed)
        except _Refused as refused:
            self._to_queued(workspace, message, refused.message)
            raise
        except BaseException as exc:  # noqa: BLE001 - unknown means unknown
            self._to_unknown(workspace, message, _explain(exc))
            raise

        if not confirmed:
            # The text was accepted by the terminal but nothing will ACK it.
            self._to_unknown(
                workspace,
                message,
                f"agent {session.agent!r} has no hook integration; delivery is unconfirmed",
            )
            refreshed = self.store.messages.find(message.id) or message
            return DispatchOutcome(
                message=refreshed,
                confirmation="delivery_unknown",
                reason="agent has no hook integration",
            )
        return self._after_delivery(workspace, message, session)

    def _deliver(self, message: Message, session: Session, *, allow_unconfirmed: bool) -> bool:
        """Type the message. Returns whether an ACK is expected at all."""
        definition = AGENT_DEFINITIONS.get(session.agent)
        integration = bool(definition and definition.integration)
        terminal = self.lifecycle.find_terminal(session)
        if terminal is None:
            raise _Refused(
                f"session {session.name!r} has no runtime terminal",
                "run `agentctl recover` first",
            )
        if not terminal.live:
            raise _Refused(
                f"session {session.name!r} is finished ({terminal.reason or 'exited'})",
                "create a new session for new work",
            )
        # Measured: a send that arrives while the agent is still working is
        # typed into the composer and never submitted, so it silently never runs.
        # Waiting for idle is the only reliable delivery condition.
        if terminal.reported and terminal.status not in (None, "idle"):
            raise _Refused(
                f"session {session.name!r} is {terminal.status} and cannot take a new message",
                "the message stays queued; dispatch resumes when the worker is idle",
            )
        if not integration and not allow_unconfirmed:
            # No harness hook means no ACK will ever arrive. Typing anyway is
            # still useful, but the outcome must stay honest, so it needs an
            # explicit opt-in rather than happening silently.
            raise _Refused(
                f"agent {session.agent!r} has no verified hook integration, so delivery cannot be confirmed",
                "the message stays queued; install its integration with "
                f"`agentctl integrations install {session.agent}`, or dispatch with "
                "--unconfirmed to type it and record delivery_unknown",
            )
        try:
            self.runtime.send_input(terminal.address, compose(message.payload, message.id))
        except RuntimeFailure as exc:
            # The runtime refused before accepting the text (blocked, finished,
            # finished terminal). Nothing was typed, so the message may be
            # retried safely.
            raise _Refused(exc.message, exc.hint or "") from exc
        return bool(integration)

    def _after_delivery(self, workspace: Workspace, message: Message, session: Session) -> DispatchOutcome:
        """Record that the transport accepted the text."""
        stamp = now()
        with transaction(self.store.conn):
            moved = self.store.messages.update_state(
                message.id,
                "dispatched",
                expected=["dispatching"],
                set_dispatched=True,
                stamp=stamp,
            )
            if not moved:  # pragma: no cover - the caller holds the row
                raise MessageStateError("message left the dispatching state unexpectedly")
            refreshed = self.store.messages.find(message.id) or message
            self.bus.emit(
                workspace,
                "message.dispatched",
                session_id=session.id,
                message_id=message.id,
                source="agentctl",
                payload={"session": session.name},
                dedupe_key=f"{message.id}:dispatched",
            )
        return DispatchOutcome(message=refreshed, confirmation="dispatched")

    def _to_queued(self, workspace: Workspace, message: Message, reason: str) -> None:
        with transaction(self.store.conn):
            self.store.messages.update_state(
                message.id,
                "queued",
                expected=["dispatching"],
                last_error=reason,
                stamp=now(),
            )

    def _to_unknown(self, workspace: Workspace, message: Message, reason: str) -> None:
        with transaction(self.store.conn):
            self.store.messages.update_state(
                message.id,
                "delivery_unknown",
                expected=["dispatching"],
                last_error=reason,
                stamp=now(),
            )
            self.bus.emit(
                workspace,
                "message.delivery_unknown",
                session_id=message.session_id,
                message_id=message.id,
                source="agentctl",
                payload={"reason": reason},
                dedupe_key=f"{message.id}:delivery_unknown",
            )

    def _fail(self, workspace: Workspace, message: Message, reason: str) -> None:
        with transaction(self.store.conn):
            self.store.messages.update_state(
                message.id, "failed", expected=[message.state], last_error=reason, stamp=now()
            )
            self.bus.emit(
                workspace,
                "message.failed",
                session_id=message.session_id,
                message_id=message.id,
                source="agentctl",
                payload={"reason": reason},
                dedupe_key=f"{message.id}:failed",
            )

    # ---------------------------------------------------------------- ACK path

    def acknowledge(self, workspace: Workspace, message_id: str, *, source: str = "hook") -> AckResult:
        """Record that the worker harness received this exact message.

        Idempotent by construction: a repeated hook delivery finds the message
        already past `dispatched` and reports `changed: False` instead of
        emitting a second logical ACK.
        """
        message = self._require(workspace, message_id)
        stamp = now()
        with transaction(self.store.conn):
            moved = self.store.messages.update_state(
                message.id,
                "acknowledged",
                expected=["dispatched", "delivery_unknown"],
                set_acknowledged=True,
                stamp=stamp,
            )
            refreshed = self.store.messages.find(message.id) or message
            if not moved:
                return AckResult(message=refreshed, changed=False, event_created=False)
            self.bus.emit(
                workspace,
                "message.acknowledged",
                session_id=message.session_id,
                message_id=message.id,
                source=source,
                payload={"acknowledged_at": stamp},
                dedupe_key=f"{message.id}:acknowledged",
            )
            return AckResult(message=refreshed, changed=True, event_created=True)

    def mark_running(self, workspace: Workspace, message_id: str, *, source: str = "hook") -> Message:
        message = self._require(workspace, message_id)
        with transaction(self.store.conn):
            self.store.messages.update_state(
                message.id, "running", expected=["acknowledged", "dispatched"], stamp=now()
            )
            return self.store.messages.find(message.id) or message

    def complete(
        self,
        workspace: Workspace,
        message_id: str,
        *,
        source: str = "hook",
        result_summary: str | None = None,
        failed: bool = False,
        turn_index: int | None = None,
    ) -> Message:
        """Close out a message's execution turn."""
        message = self._require(workspace, message_id)
        target = "failed" if failed else "completed"
        stamp = now()
        with transaction(self.store.conn):
            moved = self.store.messages.update_state(
                message.id,
                target,
                expected=["dispatched", "acknowledged", "running", "delivery_unknown"],
                set_completed=True,
                stamp=stamp,
            )
            refreshed = self.store.messages.find(message.id) or message
            if not moved:
                # A duplicate completion must not produce a second event.
                return refreshed
            self.bus.emit(
                workspace,
                "turn.failed" if failed else "turn.completed",
                session_id=message.session_id,
                message_id=message.id,
                source=source,
                payload={
                    "result": result_summary,
                    "turn_index": turn_index,
                    "message_state": target,
                },
                dedupe_key=f"{message.id}:{'failed' if failed else 'completed'}",
            )
            self.bus.emit(
                workspace,
                "message.failed" if failed else "message.completed",
                session_id=message.session_id,
                message_id=message.id,
                source="agentctl",
                payload={"result": result_summary},
                dedupe_key=f"{message.id}:message_{'failed' if failed else 'completed'}",
            )
            return refreshed

    # ------------------------------------------------------------ housekeeping

    def cancel(self, workspace: Workspace, message_id: str) -> Message:
        message = self._require(workspace, message_id)
        if message.state in ("dispatching",):
            raise MessageStateError(
                f"message {message.id} is being dispatched right now",
                "wait for the dispatch to finish, then cancel if it is still not completed",
            )
        with transaction(self.store.conn):
            moved = self.store.messages.update_state(
                message.id, "cancelled", expected=[message.state], stamp=now()
            )
            if not moved:
                raise MessageStateError(
                    f"message {message.id} is {message.state} and cannot be cancelled",
                    "only a message that has not completed can be cancelled",
                )
            return self.store.messages.find(message.id) or message

    def retry(self, workspace: Workspace, message_id: str) -> Message:
        """Re-queue a message that provably never reached the worker.

        `delivery_unknown` is refused on purpose: the text may already be in the
        worker's context, and running it twice could repeat a destructive tool
        call. `messages retry --force` is not offered; the operator must cancel
        and re-send deliberately.
        """
        message = self._require(workspace, message_id)
        if message.state == "delivery_unknown":
            raise MessageStateError(
                f"message {message.id} may already have reached the worker",
                "it is not safe to resend; check the worker with `agentctl read`, "
                "then cancel and send a new message if the work must be repeated",
            )
        if message.state not in ("failed", "queued"):
            raise MessageStateError(
                f"message {message.id} is {message.state}",
                "only a failed or queued message can be retried",
            )
        with transaction(self.store.conn):
            moved = self.store.messages.update_state(
                message.id, "queued", expected=[message.state], stamp=now()
            )
            if not moved:
                raise MessageStateError(
                    f"message {message.id} changed state while retrying",
                    "run `agentctl messages get` to see its current state",
                )
            self.store.messages.clear_error(message.id)
            refreshed = self.store.messages.find(message.id) or message
            refreshed.last_error = None
            return refreshed

    def recover_stuck(self, workspace: Workspace, *, older_than_seconds: float = 60.0) -> list[Message]:
        """Turn abandoned `dispatching` rows into `delivery_unknown`.

        A row is stuck when its process died between writing `dispatching` and
        writing the outcome. The only honest reading is "we do not know", so it
        becomes `delivery_unknown` and is excluded from automatic dispatch.
        """
        from agentctl.clock import elapsed_since

        stuck: list[Message] = []
        for message in self.store.messages.list_for_workspace(
            workspace.id, states=["dispatching"], limit=500
        ):
            age = elapsed_since(message.created_at)
            if age is not None and age < older_than_seconds:
                continue
            self._to_unknown(workspace, message, "dispatch was interrupted before its outcome was recorded")
            refreshed = self.store.messages.find(message.id)
            if refreshed is not None:
                stuck.append(refreshed)
        return stuck

    def expire_unacknowledged(self, workspace: Workspace, *, older_than_seconds: float = 120.0) -> list[Message]:
        """Turn a dispatched row that never got an ACK into `delivery_unknown`.

        A hook that failed, a worker whose composer swallowed the text, or a
        crashed harness all look the same from here: the transport accepted the
        text and the harness never confirmed it. Guessing "connected" would be
        worse than saying so.
        """
        from agentctl.clock import elapsed_since

        expired: list[Message] = []
        for message in self.store.messages.list_for_workspace(
            workspace.id, states=["dispatched"], limit=500
        ):
            age = elapsed_since(message.dispatched_at or message.created_at)
            if age is None or age < older_than_seconds:
                continue
            with transaction(self.store.conn):
                moved = self.store.messages.update_state(
                    message.id,
                    "delivery_unknown",
                    expected=["dispatched"],
                    last_error="dispatch was accepted but no harness ACK arrived",
                    stamp=now(),
                )
                if not moved:
                    continue
                self.bus.emit(
                    workspace,
                    "message.delivery_unknown",
                    session_id=message.session_id,
                    message_id=message.id,
                    source="agentctl",
                    payload={"reason": "no harness ACK within the acknowledgement window"},
                    dedupe_key=f"{message.id}:delivery_unknown",
                )
            refreshed = self.store.messages.find(message.id)
            if refreshed is not None:
                expired.append(refreshed)
        return expired

    def next_queued(self, session_id: str) -> Message | None:
        return self.store.messages.next_queued_for_session(session_id)

    def active(self, session_id: str) -> Message | None:
        return self.store.messages.active_for_session(session_id)

    # ------------------------------------------------------------------ reads

    def get(self, workspace: Workspace, message_id: str) -> Message:
        message = self.store.messages.find_in_workspace(workspace.id, message_id)
        if message is None:
            raise MessageNotFound(
                f"no message {message_id!r} in workspace {workspace.id}",
                "run `agentctl messages list` to see this workspace's messages",
            )
        return message

    def list(
        self,
        workspace: Workspace,
        *,
        states: list[str] | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[Message]:
        return self.store.messages.list_for_workspace(
            workspace.id, states=states, session_id=session_id, limit=limit
        )

    def _require(self, workspace: Workspace, message_id: str) -> Message:
        message = self.store.messages.find_in_workspace(workspace.id, message_id)
        if message is None:
            raise MessageNotFound(
                f"no message {message_id!r} in workspace {workspace.id}",
                "run `agentctl messages list`",
            )
        return message


class _Refused(AgentctlError):
    """Transport refused before the text was accepted; safe to retry."""

    code = "message_refused"
    exit_code = EXIT_BLOCKED


def _explain(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message
    text = str(exc).strip()
    return text or type(exc).__name__


__all__ = [
    "AckResult",
    "DispatchOutcome",
    "MessageService",
    "compose",
    "find_marker",
    "marker_for",
]
