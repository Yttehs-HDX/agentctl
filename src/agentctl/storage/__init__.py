"""Storage package: database bootstrap plus repositories."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from agentctl.paths import database_path
from agentctl.storage.database import SCHEMA_VERSION, connect, initialize, transaction
from agentctl.storage.repository import (
    LeaderRepository,
    SessionRepository,
    WorkspaceRepository,
)


def open_connection(path=None) -> sqlite3.Connection:
    """A ready connection; creates the file and migrates it on first use."""
    conn = connect(path or database_path())
    try:
        initialize(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def open_database(path=None):
    """Yield a ready connection and close it afterwards."""
    conn = open_connection(path)
    try:
        yield conn
    finally:
        conn.close()


class Store:
    """Bundle of the repositories over one open connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.workspaces = WorkspaceRepository(conn)
        self.sessions = SessionRepository(conn)
        self.leaders = LeaderRepository(conn)


__all__ = [
    "SCHEMA_VERSION",
    "Store",
    "open_connection",
    "open_database",
    "transaction",
]
