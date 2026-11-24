"""Hook entry points: harness events in, agentctl facts out.

Every hook invocation is a short CLI process. It resolves the workspace from the
environment the worker was launched with (`AGENTCTL_WORKSPACE_ID`), records the
fact, and prints whatever the harness expects on stdout. Nothing here waits for
a Leader, and nothing here can block for long: a hook that hangs stalls the
worker's own turn.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass

from agentctl.clock import now
from agentctl.errors import AgentctlError, UsageError
from agentctl.events.bus import EventBus
from agentctl.messaging.service import find_marker
from agentctl.storage.models import Workspace

CLAUDE_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "SessionEnd",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "SubagentStop",
    "PreCompact",
}


@dataclass(slots=True)
class HookContext:
    workspace: Workspace
    session_id: str | None
    bus: EventBus

    def session_name(self) -> str | None:
        if not self.session_id:
            return None
        session = self.bus.store.sessions.find_by_id(self.session_id)
        return session.name if session else None


def resolve_workspace(store, workspace_id: str | None) -> Workspace:
    if not workspace_id:
        raise UsageError(
            "AGENTCTL_WORKSPACE_ID is not set",
            "this hook must run inside an agentctl-created session",
        )
    workspace = store.workspaces.find_by_id(workspace_id)
    if workspace is None:
        raise UsageError(
            f"workspace {workspace_id} is not registered",
            "run `agentctl setup` in that project first",
        )
    return workspace


def record_session_started(store, workspace: Workspace, session_id: str | None, *, agent: str | None) -> dict:
    bus = EventBus(store)
    session = store.sessions.find_by_id(session_id) if session_id else None
    if session is None:
        raise UsageError(
            f"session {session_id!r} is not in the registry",
            "the hook was installed for a session agentctl no longer knows",
        )
    result = bus.emit(
        workspace,
        "session.ready",
        session_id=session.id,
        source=agent or session.agent,
        payload={"session": session.name, "agent": session.agent},
        dedupe_key=f"{session.id}:ready",
    )
    return {"created": result.created, "session": session.name}


def record_message_received(
    store,
    workspace: Workspace,
    session_id: str | None,
    *,
    message_id: str | None,
    source: str,
) -> dict:
    if not message_id:
        return {"acknowledged": False, "reason": "no agentctl marker in the text"}
    from agentctl.messaging.service import MessageService
    from agentctl.runtime.herdr_core import HerdrCoreAdapter
    from agentctl.session.lifecycle import LifecycleManager

    runtime = HerdrCoreAdapter()
    lifecycle = LifecycleManager(store, runtime)
    service = MessageService(store, runtime, lifecycle, bus=EventBus(store))
    message = store.messages.find_in_workspace(workspace.id, message_id)
    if message is None:
        raise UsageError(
            f"no message {message_id!r} in this workspace",
            "the marker in the prompt does not name a message this workspace queued",
        )
    # The row decides which session the ACK belongs to. The hook's own session
    # id is only a cross-check, so a worker that received text addressed to it
    # can never mark another session's message delivered.
    if session_id and session_id != message.session_id:
        raise UsageError(
            f"message {message_id} belongs to session {message.session_id}, not {session_id}",
            "the text was pasted into the wrong terminal; do not acknowledge it here",
        )
    ack = service.acknowledge(workspace, message_id, source=source)
    service.mark_running(workspace, message_id, source=source)
    return {
        "acknowledged": ack.changed,
        "message": ack.message.state,
        "session": message.session_id,
    }


def record_turn_completed(
    store,
    workspace: Workspace,
    session_id: str | None,
    *,
    message_id: str | None,
    source: str,
    result: str | None = None,
) -> dict:
    from agentctl.messaging.service import MessageService
    from agentctl.runtime.herdr_core import HerdrCoreAdapter
    from agentctl.session.lifecycle import LifecycleManager

    bus = EventBus(store)
    runtime = HerdrCoreAdapter()
    lifecycle = LifecycleManager(store, runtime)
    service = MessageService(store, runtime, lifecycle, bus=bus)
    if message_id:
        found = store.messages.find_in_workspace(workspace.id, message_id)
        if found is None:
            raise UsageError(f"no message {message_id!r} in this workspace")
        message = service.complete(
            workspace,
            message_id,
            source=source,
            result_summary=result or None,
            failed=False,
        )
        return {"completed": True, "message": message.state}
    # No message in flight: still record the turn, without inventing a binding.
    if session_id:
        bus.emit(
            workspace,
            "turn.completed",
            session_id=session_id,
            source=source,
            payload={"result": result, "message_id": None},
            dedupe_key=f"{session_id}:{now()}:turn.completed",
        )
    return {"completed": True, "message": None}


def record_turn_failed(
    store,
    workspace: Workspace,
    session_id: str | None,
    *,
    message_id: str | None,
    source: str,
    reason: str | None = None,
) -> dict:
    from agentctl.messaging.service import MessageService
    from agentctl.runtime.herdr_core import HerdrCoreAdapter
    from agentctl.session.lifecycle import LifecycleManager

    bus = EventBus(store)
    runtime = HerdrCoreAdapter()
    lifecycle = LifecycleManager(store, runtime)
    service = MessageService(store, runtime, lifecycle, bus=bus)
    if message_id:
        if store.messages.find_in_workspace(workspace.id, message_id) is None:
            raise UsageError(f"no message {message_id!r} in this workspace")
        message = service.complete(
            workspace, message_id, source=source, result_summary=reason, failed=True
        )
        return {"failed": True, "message": message.state}
    if session_id:
        bus.emit(
            workspace,
            "turn.failed",
            session_id=session_id,
            source=source,
            payload={"reason": reason},
            dedupe_key=f"{session_id}:{now()}:turn.failed",
        )
    return {"failed": True, "message": None}


def claude_hook(store, payload: dict, *, env_workspace: str | None, env_session: str | None) -> dict:
    """Handle one Claude Code hook payload and return what to print.

    The hook runs inside the worker terminal, so identity comes from the
    environment agentctl put there, not from `payload["session_id"]` (which is
    Claude's own id). The returned object is printed as JSON on stdout; only the
    `Stop` reply can wake the model.
    """
    event = payload.get("hook_event_name") or ""
    if not event:
        raise UsageError("hook payload has no hook_event_name")
    workspace = resolve_workspace(store, env_workspace)
    session_id = env_session

    if event == "SessionStart":
        record_session_started(store, workspace, session_id, agent="claude")
        return {"continue": True}

    if event == "UserPromptSubmit":
        text = payload.get("prompt") or ""
        message_id = find_marker(text)
        result = record_message_received(
            store, workspace, session_id, message_id=message_id, source="claude"
        )
        return {"continue": True, "agentctl": result}

    if event == "Stop":
        message_id = _in_flight_message(store, session_id)
        record_turn_completed(
            store,
            workspace,
            session_id,
            message_id=message_id,
            source="claude",
            result=payload.get("last_assistant_message"),
        )
        # A notice is acked only when it is actually delivered, so a Stop that
        # delivers nothing leaves the row for the next attempt.
        if payload.get("stop_hook_active"):
            return {}
        notice = pending_notice(store, workspace)
        if not notice:
            return {}
        return {"decision": "block", "reason": notice}

    if event == "SessionEnd":
        if session_id:
            EventBus(store).emit(
                workspace,
                "session.exited",
                session_id=session_id,
                source="claude",
                payload={"reason": payload.get("reason")},
                dedupe_key=f"{session_id}:hook-exited",
            )
        return {"continue": True}

    # Any other hook is installed for observability only; never interfere.
    return {"continue": True}


def _in_flight_message(store, session_id: str | None) -> str | None:
    if not session_id:
        return None
    active = store.messages.active_for_session(session_id)
    return active.id if active is not None else None


def pending_notice(store, workspace: Workspace, *, limit: int = 5) -> str:
    """One-line summary of Leader-relevant events waiting in the inbox."""
    bus = EventBus(store)
    items = bus.pending_for_leader(workspace, limit=limit)
    if not items:
        return ""
    lines: list[str] = []
    for item in items:
        event = item.event
        session = store.sessions.find_by_id(event.session_id) if event.session_id else None
        who = session.name if session else "-"
        message = f" message={event.message_id}" if event.message_id else ""
        detail = ""
        if event.payload.get("result"):
            detail = f" result={event.payload['result']}"
        lines.append(
            f"[agentctl event] worker={who}{message} event={event.event}"
            f" severity={event.severity}{detail} inbox={item.id}"
        )
    return "\n".join(lines)
