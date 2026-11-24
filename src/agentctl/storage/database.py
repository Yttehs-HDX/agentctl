"""SQLite persistence: connection, schema and migrations.

One database per user (see agentctl.paths). WAL keeps readers from blocking a
writer; every state transition runs in an explicit transaction.

Schema history
--------------
1  v0.1: workspaces + sessions.
2  v0.2: leader lease, message registry, event log, per-consumer cursors.
3  v0.2: integration state and leases gain their instance columns.
4  v0.3: those tables are dropped. Herdr owns messaging, events and liveness;
        agentctl keeps only workspace identity, sessions and the Leader binding.

`initialize()` is safe to run on any older file: every object is created with
`IF NOT EXISTS` and every column addition checks `PRAGMA table_info` first, so a
migration that already ran is a no-op.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agentctl.errors import ConfigError

SCHEMA_VERSION = 4

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
CREATE TABLE IF NOT EXISTS leader_bindings (
    workspace_id TEXT PRIMARY KEY,
    leader_id TEXT NOT NULL,
    terminal TEXT,
    native_session_id TEXT,
    attached_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
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


#: Tables v0.2 introduced and v0.3 removed. Dropping them is the upgrade: their
#: content duplicated Herdr state, so nothing in them is worth migrating.
_OBSOLETE_TABLES = (
    "messages",
    "events",
    "inbox",
    "outbox",
    "consumer_offsets",
    "integrations",
    "leader_leases",
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
}


def _migrate(conn: sqlite3.Connection) -> None:
    # The agent catalog is computed at call time; the old persisted mirror had
    # no reader. Drop it so an upgraded registry.db carries no dead table.
    conn.execute("DROP TABLE IF EXISTS agent_catalog")
    for table in _OBSOLETE_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, sql_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


class transaction:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK context manager.

    `BEGIN IMMEDIATE` takes the write lock up front, so a read-modify-write
    cannot interleave with another process. Nested use becomes a savepoint.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.nested = False

    def __enter__(self) -> sqlite3.Connection:
        if self.conn.in_transaction:
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
