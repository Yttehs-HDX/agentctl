"""SQLite persistence: connection, schema and migrations.

One database per user (see agentctl.paths). WAL keeps readers from blocking a
writer; every state transition runs in an explicit transaction.

Schema history
--------------
1  v0.1: workspaces + sessions.
2  v0.2: leader lease, message registry, event log, per-consumer cursors.
3  v0.2: integration state and leases gain their instance columns.

`initialize()` is safe to run on any older file: every object is created with
`IF NOT EXISTS` and every column addition checks `PRAGMA table_info` first, so a
migration that already ran is a no-op.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agentctl.errors import ConfigError

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL UNIQUE,
    leader_id TEXT NOT NULL UNIQUE,
    default_agent TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    agent TEXT NOT NULL,
    cwd TEXT NOT NULL,
    mode TEXT NOT NULL,
    hc_terminal_id TEXT,
    hc_name TEXT,
    native_session_id TEXT,
    native_session_dir TEXT,
    worktree_path TEXT,
    worktree_branch TEXT,
    git_root TEXT,
    runtime_started_at TEXT,
    status TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS sessions_one_live_name
    ON sessions(workspace_id, name) WHERE deleted_at IS NULL;

-- One row per workspace: the lease is a single-writer row, so SQLite's own
-- transaction isolation arbitrates concurrent claims without a lock file.
CREATE TABLE IF NOT EXISTS leader_leases (
    workspace_id TEXT PRIMARY KEY,
    leader_id TEXT NOT NULL,
    leader_instance_id TEXT NOT NULL,
    leader_epoch INTEGER NOT NULL,
    lease_token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    acknowledged_at TEXT,
    completed_at TEXT,
    last_error TEXT,
    ack_token TEXT,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS messages_by_session
    ON messages(workspace_id, session_id, created_at);
CREATE INDEX IF NOT EXISTS messages_by_state
    ON messages(workspace_id, state, created_at);
-- The same Worker may run messages after each other, never at the same time,
-- so at most one *in-flight* message per session may exist. `queued` rows are
-- deliberately outside this index: a Leader sends work ahead of time and the
-- worker picks it up one message at a time.
DROP INDEX IF EXISTS messages_one_active_per_session;
CREATE UNIQUE INDEX IF NOT EXISTS messages_one_inflight_per_session
    ON messages(session_id)
    WHERE state IN ('dispatching','dispatched','acknowledged','running','delivery_unknown');

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    event TEXT NOT NULL,
    session_id TEXT,
    message_id TEXT,
    leader_epoch INTEGER,
    source TEXT,
    severity TEXT NOT NULL DEFAULT 'info',
    payload TEXT NOT NULL,
    dedupe_key TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS events_dedupe
    ON events(workspace_id, event, dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_by_workspace
    ON events(workspace_id, created_at);

-- Delivered-to-Leader queue: one row per event that must reach a Leader turn.
CREATE TABLE IF NOT EXISTS inbox (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    workspace_id TEXT NOT NULL,
    leader_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by TEXT,
    acked_at TEXT,
    FOREIGN KEY(event_id) REFERENCES events(id)
);

CREATE INDEX IF NOT EXISTS inbox_pending
    ON inbox(workspace_id, state, created_at);

-- Work the daemon must perform and survive a restart.
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    not_before TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS outbox_due
    ON outbox(workspace_id, state, not_before);

-- Named read positions, so a consumer that restarts resumes where it stopped.
CREATE TABLE IF NOT EXISTS consumer_offsets (
    consumer TEXT PRIMARY KEY,
    position TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Installed harness adapters: which agent has which hook files wired up.
CREATE TABLE IF NOT EXISTS integrations (
    workspace_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    scope TEXT NOT NULL,
    hook_path TEXT,
    settings_path TEXT,
    -- The harness config root the hook was installed into. A worker launched
    -- by agentctl is pointed at exactly this directory, so install and launch
    -- cannot disagree about where the hook lives.
    config_dir TEXT,
    capability TEXT NOT NULL,
    version TEXT,
    installed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(workspace_id, agent, scope)
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    # executescript() commits implicitly, so the DDL runs outside a transaction
    # and only the version row is transactional.
    conn.executescript(SCHEMA)
    _migrate(conn)
    with transaction(conn):
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        else:
            stored = int(row["value"])
            if stored > SCHEMA_VERSION:
                raise ConfigError(
                    f"registry schema version {stored} is newer than this agentctl supports "
                    f"({SCHEMA_VERSION})",
                    "upgrade agentctl, or back up and remove registry.db and run `agentctl setup`",
                )
            if stored != SCHEMA_VERSION:
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key='schema_version'", (str(SCHEMA_VERSION),)
                )


# Columns added after a release; each is created on demand so an existing
# registry.db keeps working. The SQL type must stay append-only: never change a
# name here without a data migration.
_ADDED_COLUMNS = {
    "sessions": {
        "worktree_path": "TEXT",
        "worktree_branch": "TEXT",
        "git_root": "TEXT",
        "runtime_started_at": "TEXT",
    },
    "messages": {
        "ack_token": "TEXT",
    },
    "leader_leases": {
        "released_at": "TEXT",
    },
    "events": {
        "severity": "TEXT NOT NULL DEFAULT 'info'",
    },
    "integrations": {
        "config_dir": "TEXT",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    # The agent catalog is computed at call time; the old persisted mirror had
    # no reader. Drop it so an upgraded registry.db carries no dead table.
    conn.execute("DROP TABLE IF EXISTS agent_catalog")
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, sql_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
    # Older registries allowed several live messages per session. Reconcile by
    # failing only the extras, never by deleting payloads: history stays intact.
    _enforce_single_active_message(conn)


def _enforce_single_active_message(conn: sqlite3.Connection) -> None:
    index = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='messages_one_inflight_per_session'"
    ).fetchone()
    if index is None:
        return
    duplicates = conn.execute(
        """
        SELECT session_id FROM messages
         WHERE state IN ('dispatching','dispatched','acknowledged','running','delivery_unknown')
         GROUP BY session_id HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in duplicates:
        rows = conn.execute(
            """
            SELECT id FROM messages
             WHERE session_id = ?
               AND state IN ('dispatching','dispatched','acknowledged','running','delivery_unknown')
             ORDER BY created_at DESC, rowid DESC
            """,
            (row["session_id"],),
        ).fetchall()
        # Keep the newest claimable message; older ones were superseded.
        for stale in rows[1:]:
            conn.execute(
                "UPDATE messages SET state='failed', last_error=? WHERE id=?",
                ("superseded by a newer message for the same session", stale["id"]),
            )


class transaction:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK context manager.

    `BEGIN IMMEDIATE` takes the write lock up front, so a read-modify-write on
    the lease row cannot interleave with another process's claim. Nested use
    becomes a savepoint, which lets services compose transactional helpers.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.depth = 0

    def __enter__(self) -> sqlite3.Connection:
        if self.conn.in_transaction:
            self.depth = 0
            self.conn.execute("SAVEPOINT agentctl_nested")
            self.nested = True
        else:
            self.conn.execute("BEGIN IMMEDIATE")
            self.nested = False
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.nested:
            if exc_type is None:
                self.conn.execute("RELEASE agentctl_nested")
            else:
                self.conn.execute("ROLLBACK TO agentctl_nested")
                self.conn.execute("RELEASE agentctl_nested")
            return
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
