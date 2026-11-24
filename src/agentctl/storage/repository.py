"""Row mapping helpers between SQLite and the domain records."""

from __future__ import annotations

import json
import sqlite3

from agentctl.storage.models import (
    Event,
    InboxItem,
    Integration,
    LeaderLease,
    Message,
    OutboxItem,
    Session,
    Workspace,
)


def _row_to_workspace(row: sqlite3.Row) -> Workspace:
    return Workspace(
        id=row["id"],
        root_path=row["root_path"],
        leader_id=row["leader_id"],
        default_agent=row["default_agent"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        agent=row["agent"],
        cwd=row["cwd"],
        mode=row["mode"],
        hc_terminal_id=row["hc_terminal_id"],
        hc_name=row["hc_name"],
        native_session_id=row["native_session_id"],
        native_session_dir=row["native_session_dir"],
        worktree_path=row["worktree_path"],
        worktree_branch=row["worktree_branch"],
        git_root=row["git_root"],
        runtime_started_at=row["runtime_started_at"],
        status=row["status"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


class WorkspaceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def find_by_root(self, root_path: str) -> Workspace | None:
        row = self.conn.execute(
            "SELECT * FROM workspaces WHERE root_path = ?", (root_path,)
        ).fetchone()
        return _row_to_workspace(row) if row else None

    def list_all(self) -> list[Workspace]:
        rows = self.conn.execute("SELECT * FROM workspaces ORDER BY root_path").fetchall()
        return [_row_to_workspace(row) for row in rows]

    def find_by_id(self, workspace_id: str) -> Workspace | None:
        row = self.conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        return _row_to_workspace(row) if row else None

    def insert(self, workspace: Workspace) -> None:
        self.conn.execute(
            """
            INSERT INTO workspaces(id, root_path, leader_id, default_agent, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                workspace.id,
                workspace.root_path,
                workspace.leader_id,
                workspace.default_agent,
                workspace.created_at,
                workspace.updated_at,
            ),
        )

    def update_root(self, workspace_id: str, root_path: str, updated_at: str) -> None:
        self.conn.execute(
            "UPDATE workspaces SET root_path = ?, updated_at = ? WHERE id = ?",
            (root_path, updated_at, workspace_id),
        )

    def set_default_agent(self, workspace_id: str, agent: str | None, updated_at: str) -> None:
        self.conn.execute(
            "UPDATE workspaces SET default_agent = ?, updated_at = ? WHERE id = ?",
            (agent, updated_at, workspace_id),
        )

    def touch(self, workspace_id: str, updated_at: str) -> None:
        self.conn.execute("UPDATE workspaces SET updated_at = ? WHERE id = ?", (updated_at, workspace_id))


class SessionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def find_by_id(self, session_id: str) -> Session | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _row_to_session(row) if row else None

    def find_in_workspace(self, workspace_id: str, name: str) -> Session | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE workspace_id = ? AND name = ? AND deleted_at IS NULL",
            (workspace_id, name),
        ).fetchone()
        return _row_to_session(row) if row else None

    def list_for_workspace(self, workspace_id: str, *, include_deleted: bool = False) -> list[Session]:
        if include_deleted:
            rows = self.conn.execute(
                "SELECT * FROM sessions WHERE workspace_id = ? ORDER BY created_at", (workspace_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM sessions WHERE workspace_id = ? AND deleted_at IS NULL ORDER BY created_at",
                (workspace_id,),
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    def list_deleted(self, workspace_id: str) -> list[Session]:
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE workspace_id = ? AND deleted_at IS NOT NULL "
            "ORDER BY deleted_at",
            (workspace_id,),
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    def insert(self, session: Session) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions(
                id, workspace_id, name, agent, cwd, mode, hc_terminal_id, hc_name,
                native_session_id, native_session_dir, worktree_path, worktree_branch,
                git_root, runtime_started_at, status, last_error,
                created_at, updated_at, deleted_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.workspace_id,
                session.name,
                session.agent,
                session.cwd,
                session.mode,
                session.hc_terminal_id,
                session.hc_name,
                session.native_session_id,
                session.native_session_dir,
                session.worktree_path,
                session.worktree_branch,
                session.git_root,
                session.runtime_started_at,
                session.status,
                session.last_error,
                session.created_at,
                session.updated_at,
                session.deleted_at,
            ),
        )

    def update_runtime(
        self,
        session_id: str,
        *,
        hc_terminal_id: str | None,
        hc_name: str | None,
        native_session_id: str | None,
        native_session_dir: str | None,
        status: str,
        last_error: str | None,
        updated_at: str,
        runtime_started_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE sessions
               SET hc_terminal_id = ?, hc_name = ?, native_session_id = ?,
                   native_session_dir = ?, status = ?, last_error = ?, updated_at = ?,
                   runtime_started_at = COALESCE(?, runtime_started_at)
             WHERE id = ?
            """,
            (
                hc_terminal_id,
                hc_name,
                native_session_id,
                native_session_dir,
                status,
                last_error,
                updated_at,
                runtime_started_at,
                session_id,
            ),
        )

    def set_status(self, session_id: str, status: str, last_error: str | None, updated_at: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (status, last_error, updated_at, session_id),
        )

    def mark_deleted(self, session_id: str, status: str, deleted_at: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET status = ?, deleted_at = ?, updated_at = ?, hc_terminal_id = NULL WHERE id = ?",
            (status, deleted_at, deleted_at, session_id),
        )

    def hard_delete(self, session_id: str) -> None:
        self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


# ---------------------------------------------------------------------------
# v0.2 repositories
#
# Every state word that appears in SQL is also declared in
# `agentctl.messaging.transitions`, where it can be tested without a database.
# Only this module may translate a record into a row and back.
# ---------------------------------------------------------------------------


def _row_to_lease(row: sqlite3.Row) -> LeaderLease:
    return LeaderLease(
        workspace_id=row["workspace_id"],
        leader_id=row["leader_id"],
        leader_instance_id=row["leader_instance_id"],
        leader_epoch=row["leader_epoch"],
        lease_token=row["lease_token"],
        acquired_at=row["acquired_at"],
        renewed_at=row["renewed_at"],
        expires_at=row["expires_at"],
        released_at=row["released_at"],
    )


def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        workspace_id=row["workspace_id"],
        session_id=row["session_id"],
        sender_id=row["sender_id"],
        recipient_id=row["recipient_id"],
        payload=row["payload"],
        state=row["state"],
        attempt_count=row["attempt_count"],
        created_at=row["created_at"],
        dispatched_at=row["dispatched_at"],
        acknowledged_at=row["acknowledged_at"],
        completed_at=row["completed_at"],
        last_error=row["last_error"],
        ack_token=row["ack_token"],
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        workspace_id=row["workspace_id"],
        event=row["event"],
        session_id=row["session_id"],
        message_id=row["message_id"],
        leader_epoch=row["leader_epoch"],
        source=row["source"],
        severity=row["severity"],
        payload=json.loads(row["payload"] or "{}"),
        dedupe_key=row["dedupe_key"],
        created_at=row["created_at"],
    )


def _row_to_inbox(row: sqlite3.Row) -> InboxItem:
    return InboxItem(
        id=row["id"],
        event_id=row["event_id"],
        workspace_id=row["workspace_id"],
        leader_id=row["leader_id"],
        state=row["state"],
        created_at=row["created_at"],
        claimed_at=row["claimed_at"],
        claimed_by=row["claimed_by"],
        acked_at=row["acked_at"],
    )


def _row_to_outbox(row: sqlite3.Row) -> OutboxItem:
    return OutboxItem(
        id=row["id"],
        workspace_id=row["workspace_id"],
        kind=row["kind"],
        subject_id=row["subject_id"],
        state=row["state"],
        attempt_count=row["attempt_count"],
        not_before=row["not_before"],
        created_at=row["created_at"],
        last_error=row["last_error"],
    )


def _row_to_integration(row: sqlite3.Row) -> Integration:
    return Integration(
        workspace_id=row["workspace_id"],
        agent=row["agent"],
        scope=row["scope"],
        hook_path=row["hook_path"],
        settings_path=row["settings_path"],
        capability=row["capability"],
        version=row["version"],
        installed_at=row["installed_at"],
        updated_at=row["updated_at"],
        config_dir=row["config_dir"],
    )


class LeaseRepository:
    """The lease row is the arbiter: claims are single-statement transactions."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, workspace_id: str) -> LeaderLease | None:
        row = self.conn.execute(
            "SELECT * FROM leader_leases WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        return _row_to_lease(row) if row else None

    def upsert(self, lease: LeaderLease) -> None:
        self.conn.execute(
            """
            INSERT INTO leader_leases(
                workspace_id, leader_id, leader_instance_id, leader_epoch,
                lease_token, acquired_at, renewed_at, expires_at, released_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id) DO UPDATE SET
                leader_id = excluded.leader_id,
                leader_instance_id = excluded.leader_instance_id,
                leader_epoch = excluded.leader_epoch,
                lease_token = excluded.lease_token,
                acquired_at = excluded.acquired_at,
                renewed_at = excluded.renewed_at,
                expires_at = excluded.expires_at,
                released_at = excluded.released_at
            """,
            (
                lease.workspace_id,
                lease.leader_id,
                lease.leader_instance_id,
                lease.leader_epoch,
                lease.lease_token,
                lease.acquired_at,
                lease.renewed_at,
                lease.expires_at,
                lease.released_at,
            ),
        )

    def renew(self, workspace_id: str, lease_token: str, *, renewed_at: str, expires_at: str) -> bool:
        """Extend a lease only if the caller still holds this exact token."""
        cursor = self.conn.execute(
            """
            UPDATE leader_leases
               SET renewed_at = ?, expires_at = ?
             WHERE workspace_id = ? AND lease_token = ? AND released_at IS NULL
            """,
            (renewed_at, expires_at, workspace_id, lease_token),
        )
        return cursor.rowcount == 1

    def release(self, workspace_id: str, lease_token: str, *, released_at: str) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE leader_leases SET released_at = ?
             WHERE workspace_id = ? AND lease_token = ? AND released_at IS NULL
            """,
            (released_at, workspace_id, lease_token),
        )
        return cursor.rowcount == 1


class MessageRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def find(self, message_id: str) -> Message | None:
        row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _row_to_message(row) if row else None

    def find_in_workspace(self, workspace_id: str, message_id: str) -> Message | None:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE id = ? AND workspace_id = ?", (message_id, workspace_id)
        ).fetchone()
        return _row_to_message(row) if row else None

    def list_for_workspace(
        self, workspace_id: str, *, states: list[str] | None = None, session_id: str | None = None, limit: int = 200
    ) -> list[Message]:
        clauses = ["workspace_id = ?"]
        params: list[object] = [workspace_id]
        if states:
            clauses.append(f"state IN ({','.join('?' * len(states))})")
            params.extend(states)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_message(row) for row in rows]

    def active_for_session(self, session_id: str) -> Message | None:
        """The message this session is currently working on, if any."""
        row = self.conn.execute(
            """
            SELECT * FROM messages
             WHERE session_id = ?
               AND state IN ('dispatching','dispatched','acknowledged','running','delivery_unknown')
             ORDER BY created_at DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        return _row_to_message(row) if row else None

    def clear_error(self, message_id: str) -> None:
        self.conn.execute("UPDATE messages SET last_error = NULL WHERE id = ?", (message_id,))

    def next_queued_for_session(self, session_id: str) -> Message | None:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND state = 'queued' "
            "ORDER BY created_at, rowid LIMIT 1",
            (session_id,),
        ).fetchone()
        return _row_to_message(row) if row else None

    def insert(self, message: Message) -> None:
        self.conn.execute(
            """
            INSERT INTO messages(
                id, workspace_id, session_id, sender_id, recipient_id, payload,
                state, attempt_count, created_at, dispatched_at, acknowledged_at,
                completed_at, last_error, ack_token
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.id,
                message.workspace_id,
                message.session_id,
                message.sender_id,
                message.recipient_id,
                message.payload,
                message.state,
                message.attempt_count,
                message.created_at,
                message.dispatched_at,
                message.acknowledged_at,
                message.completed_at,
                message.last_error,
                message.ack_token,
            ),
        )

    def update_state(
        self,
        message_id: str,
        state: str,
        *,
        expected: list[str],
        last_error: str | None = None,
        set_dispatched: bool = False,
        set_acknowledged: bool = False,
        set_completed: bool = False,
        increment_attempt: bool = False,
        stamp: str,
    ) -> bool:
        """Compare-and-set a message state.

        `expected` makes the transition conditional, so a duplicate harness ACK
        or a late retry cannot move a finished message backwards.
        """
        assignments = ["state = ?"]
        params: list[object] = [state]
        if last_error is not None:
            assignments.append("last_error = ?")
            params.append(last_error)
        if set_dispatched:
            assignments.append("dispatched_at = ?")
            params.append(stamp)
        if set_acknowledged:
            assignments.append("acknowledged_at = ?")
            params.append(stamp)
        if set_completed:
            assignments.append("completed_at = ?")
            params.append(stamp)
        if increment_attempt:
            assignments.append("attempt_count = attempt_count + 1")
        params.append(message_id)
        params.extend(expected)
        cursor = self.conn.execute(
            f"UPDATE messages SET {', '.join(assignments)} "
            f"WHERE id = ? AND state IN ({','.join('?' * len(expected))})",
            params,
        )
        return cursor.rowcount == 1


class EventRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(self, event: Event) -> bool:
        """Insert; a duplicate dedupe_key is a no-op, not an error."""
        try:
            self.conn.execute(
                """
                INSERT INTO events(id, workspace_id, event, session_id, message_id,
                                   leader_epoch, source, severity, payload, dedupe_key, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.workspace_id,
                    event.event,
                    event.session_id,
                    event.message_id,
                    event.leader_epoch,
                    event.source,
                    event.severity,
                    json.dumps(event.payload),
                    event.dedupe_key,
                    event.created_at,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def find(self, event_id: str) -> Event | None:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return _row_to_event(row) if row else None

    def find_by_dedupe(self, workspace_id: str, event: str, dedupe_key: str) -> Event | None:
        row = self.conn.execute(
            "SELECT * FROM events WHERE workspace_id = ? AND event = ? AND dedupe_key = ?",
            (workspace_id, event, dedupe_key),
        ).fetchone()
        return _row_to_event(row) if row else None

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        after_id: str | None = None,
        limit: int = 100,
        events: list[str] | None = None,
    ) -> list[Event]:
        clauses = ["workspace_id = ?"]
        params: list[object] = [workspace_id]
        if after_id:
            # rowid keeps pagination stable when timestamps collide.
            clauses.append("rowid > COALESCE((SELECT rowid FROM events WHERE id = ?), 0)")
            params.append(after_id)
        if events:
            clauses.append(f"event IN ({','.join('?' * len(events))})")
            params.extend(events)
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY rowid LIMIT ?", params
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def latest_id(self, workspace_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT id FROM events WHERE workspace_id = ? ORDER BY rowid DESC LIMIT 1", (workspace_id,)
        ).fetchone()
        return row["id"] if row else None


class InboxRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(self, item: InboxItem) -> bool:
        try:
            self.conn.execute(
                """
                INSERT INTO inbox(id, event_id, workspace_id, leader_id, state, created_at,
                                  claimed_at, claimed_by, acked_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.event_id,
                    item.workspace_id,
                    item.leader_id,
                    item.state,
                    item.created_at,
                    item.claimed_at,
                    item.claimed_by,
                    item.acked_at,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def pending(self, workspace_id: str, *, limit: int = 50) -> list[InboxItem]:
        rows = self.conn.execute(
            "SELECT * FROM inbox WHERE workspace_id = ? AND state = 'pending' "
            "ORDER BY created_at, rowid LIMIT ?",
            (workspace_id, limit),
        ).fetchall()
        return [_row_to_inbox(row) for row in rows]

    def list_for_workspace(self, workspace_id: str, *, states: list[str] | None = None, limit: int = 100) -> list[InboxItem]:
        clauses = ["workspace_id = ?"]
        params: list[object] = [workspace_id]
        if states:
            clauses.append(f"state IN ({','.join('?' * len(states))})")
            params.extend(states)
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM inbox WHERE {' AND '.join(clauses)} ORDER BY created_at, rowid LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_inbox(row) for row in rows]

    def find(self, item_id: str) -> InboxItem | None:
        row = self.conn.execute("SELECT * FROM inbox WHERE id = ? OR event_id = ? LIMIT 1", (item_id, item_id)).fetchone()
        return _row_to_inbox(row) if row else None

    def claim(self, item_id: str, *, claimed_by: str, stamp: str) -> bool:
        cursor = self.conn.execute(
            "UPDATE inbox SET state='claimed', claimed_at=?, claimed_by=? WHERE id=? AND state='pending'",
            (stamp, claimed_by, item_id),
        )
        return cursor.rowcount == 1

    def ack(self, item_id: str, *, stamp: str) -> bool:
        """Idempotent: acknowledging an already-acked item reports success."""
        cursor = self.conn.execute(
            "UPDATE inbox SET state='acked', acked_at=COALESCE(acked_at, ?) WHERE id=? AND state IN ('pending','claimed')",
            (stamp, item_id),
        )
        if cursor.rowcount == 1:
            return True
        row = self.conn.execute("SELECT state FROM inbox WHERE id=?", (item_id,)).fetchone()
        return bool(row and row["state"] == "acked")

    def count_pending(self, workspace_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM inbox WHERE workspace_id = ? AND state IN ('pending','claimed')",
            (workspace_id,),
        ).fetchone()
        return int(row["n"])


class OutboxRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def enqueue(self, item: OutboxItem) -> None:
        self.conn.execute(
            """
            INSERT INTO outbox(id, workspace_id, kind, subject_id, state, attempt_count,
                               not_before, created_at, last_error)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                item.id,
                item.workspace_id,
                item.kind,
                item.subject_id,
                item.state,
                item.attempt_count,
                item.not_before,
                item.created_at,
                item.last_error,
            ),
        )

    def due(self, workspace_id: str, *, at: str, limit: int = 20) -> list[OutboxItem]:
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE workspace_id = ? AND state = 'pending' AND not_before <= ? "
            "ORDER BY not_before, rowid LIMIT ?",
            (workspace_id, at, limit),
        ).fetchall()
        return [_row_to_outbox(row) for row in rows]

    def list_for_workspace(self, workspace_id: str, *, limit: int = 100) -> list[OutboxItem]:
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE workspace_id = ? ORDER BY created_at, rowid LIMIT ?",
            (workspace_id, limit),
        ).fetchall()
        return [_row_to_outbox(row) for row in rows]

    def finish(self, item_id: str, *, state: str, last_error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE outbox SET state = ?, last_error = ? WHERE id = ?", (state, last_error, item_id)
        )

    def defer(self, item_id: str, *, not_before: str, last_error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE outbox SET not_before = ?, attempt_count = attempt_count + 1, last_error = ? WHERE id = ?",
            (not_before, last_error, item_id),
        )


class ConsumerOffsetRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, consumer: str) -> str | None:
        row = self.conn.execute(
            "SELECT position FROM consumer_offsets WHERE consumer = ?", (consumer,)
        ).fetchone()
        return row["position"] if row else None

    def set(self, consumer: str, position: str, *, stamp: str) -> None:
        self.conn.execute(
            """
            INSERT INTO consumer_offsets(consumer, position, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(consumer) DO UPDATE SET position = excluded.position, updated_at = excluded.updated_at
            """,
            (consumer, position, stamp),
        )


class IntegrationRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, integration: Integration) -> None:
        self.conn.execute(
            """
            INSERT INTO integrations(workspace_id, agent, scope, hook_path, settings_path,
                                     config_dir, capability, version, installed_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, agent, scope) DO UPDATE SET
                hook_path = excluded.hook_path,
                settings_path = excluded.settings_path,
                config_dir = excluded.config_dir,
                capability = excluded.capability,
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (
                integration.workspace_id,
                integration.agent,
                integration.scope,
                integration.hook_path,
                integration.settings_path,
                integration.config_dir,
                integration.capability,
                integration.version,
                integration.installed_at,
                integration.updated_at,
            ),
        )

    def get(self, workspace_id: str, agent: str, scope: str) -> Integration | None:
        row = self.conn.execute(
            "SELECT * FROM integrations WHERE workspace_id = ? AND agent = ? AND scope = ?",
            (workspace_id, agent, scope),
        ).fetchone()
        return _row_to_integration(row) if row else None

    def list(self, workspace_id: str) -> list[Integration]:
        rows = self.conn.execute(
            "SELECT * FROM integrations WHERE workspace_id = ? ORDER BY agent, scope", (workspace_id,)
        ).fetchall()
        return [_row_to_integration(row) for row in rows]

    def delete(self, workspace_id: str, agent: str, scope: str) -> None:
        self.conn.execute(
            "DELETE FROM integrations WHERE workspace_id = ? AND agent = ? AND scope = ?",
            (workspace_id, agent, scope),
        )
