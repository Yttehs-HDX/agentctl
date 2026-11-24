"""Row mapping helpers between SQLite and the domain records."""

from __future__ import annotations

import sqlite3
from agentctl.storage.models import Session, Workspace


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
