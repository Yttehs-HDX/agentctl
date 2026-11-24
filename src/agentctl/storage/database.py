"""SQLite persistence: connection, schema and repositories.

One database per user (see agentctl.paths). WAL keeps readers from blocking a
writer; every state transition runs in an explicit transaction.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agentctl.errors import ConfigError

SCHEMA_VERSION = 1

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
        elif int(row["value"]) != SCHEMA_VERSION:
            raise ConfigError(
                f"registry schema version {row['value']} is not supported (expected {SCHEMA_VERSION})",
                "back up and remove registry.db, then run `agentctl setup`",
            )


# Columns added after the first release; each is created on demand so an
# existing registry.db keeps working.
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
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, sql_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


class transaction:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK context manager."""

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
