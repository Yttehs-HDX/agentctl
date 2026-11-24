"""Events: the durable record of what happened, and what must reach a Leader.

Two separate concerns share this module on purpose, because they must be
written in one transaction to stay consistent:

- the **event log** is an append-only history of facts (session created, turn
  completed, message acknowledged);
- the **inbox** holds the subset of events a Leader must act on, one row per
  event, so "the Leader was asleep" degrades to "rows waiting" instead of a
  lost notification.

Deduplication is by `(workspace, event, dedupe_key)`. Harness hooks are
external and re-run on reconnect, so a duplicate emission is the normal case,
not an error: the second insert is a no-op and returns the first event.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentctl.clock import new_id, now
from agentctl.storage.database import transaction
from agentctl.storage.models import Event, InboxItem, Workspace

# Every event agentctl emits. The words are part of the surface documented in
# docs/events.md; adding one is additive, renaming one is a breaking change.
EVENT_NAMES = frozenset(
    {
        "session.created",
        "session.ready",
        "session.status_changed",
        "session.exited",
        "message.queued",
        "message.dispatched",
        "message.acknowledged",
        "message.delivery_unknown",
        "message.completed",
        "message.failed",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "worker.blocked",
        "worker.interrupted",
        "leader.attached",
        "leader.detached",
        "leader.expired",
        "leader.superseded",
    }
)

# Events a Leader turn should be told about. `turn.started` is deliberately
# absent: it is bookkeeping for the message registry, not news for a Leader, and
# waking a Leader to say "a worker began" would be noise.
LEADER_RELEVANT = frozenset(
    {
        "turn.completed",
        "turn.failed",
        "message.failed",
        "message.delivery_unknown",
        "worker.blocked",
        "worker.interrupted",
        "session.exited",
        "leader.superseded",
    }
)

# Severity is carried on the event so a Leader can triage without parsing
# payloads: warnings and errors are the ones worth an immediate turn.
_SEVERITY = {
    "turn.failed": "warning",
    "message.failed": "error",
    "message.delivery_unknown": "warning",
    "worker.blocked": "warning",
    "worker.interrupted": "warning",
    "session.exited": "warning",
    "leader.superseded": "error",
    "leader.expired": "warning",
}


@dataclass(slots=True)
class Emitted:
    event: Event
    created: bool
    inbox: InboxItem | None

    def to_dict(self) -> dict[str, object]:
        return {
            "event": self.event.to_dict(),
            "created": self.created,
            "inbox_id": self.inbox.id if self.inbox else None,
        }


class EventBus:
    """Write-side of the event log and inbox."""

    def __init__(self, store) -> None:
        self.store = store

    def emit(
        self,
        workspace: Workspace,
        event: str,
        *,
        session_id: str | None = None,
        message_id: str | None = None,
        source: str | None = None,
        payload: dict[str, object] | None = None,
        dedupe_key: str | None = None,
        leader_epoch: int | None = None,
        severity: str | None = None,
    ) -> Emitted:
        if event not in EVENT_NAMES:
            # An unknown name almost always means a typo in a hook, which would
            # otherwise be invisible forever. Fail loudly instead.
            raise ValueError(f"unknown event name {event!r}")
        with transaction(self.store.conn):
            record = Event(
                id=new_id("evt"),
                workspace_id=workspace.id,
                event=event,
                session_id=session_id,
                message_id=message_id,
                leader_epoch=leader_epoch,
                source=source,
                severity=severity or _SEVERITY.get(event, "info"),
                payload=payload or {},
                dedupe_key=dedupe_key,
                created_at=now(),
            )
            created = self.store.events.insert(record)
            if not created:
                existing = (
                    self.store.events.find_by_dedupe(workspace.id, event, record.dedupe_key)
                    if dedupe_key
                    else None
                )
                return Emitted(event=existing or record, created=False, inbox=None)
            if event in LEADER_RELEVANT:
                item = InboxItem(
                    id=new_id("inb"),
                    event_id=record.id,
                    workspace_id=workspace.id,
                    leader_id=workspace.leader_id,
                    state="pending",
                    created_at=record.created_at,
                    claimed_at=None,
                    claimed_by=None,
                    acked_at=None,
                )
                self.store.inbox.insert(item)
                return Emitted(event=record, created=True, inbox=item)
            return Emitted(event=record, created=True, inbox=None)

    # ------------------------------------------------------------------ reads

    def list(
        self,
        workspace: Workspace,
        *,
        after_id: str | None = None,
        limit: int = 100,
        names: list[str] | None = None,
    ) -> list[Event]:
        return self.store.events.list_for_workspace(
            workspace.id, after_id=after_id, limit=limit, events=names
        )

    def pending_for_leader(self, workspace: Workspace, *, limit: int = 50) -> list[InboxItem]:
        items = self.store.inbox.pending(workspace.id, limit=limit)
        for item in items:
            item.event = self.store.events.find(item.event_id)
        return [item for item in items if item.event is not None]
