import sqlite3
from pathlib import Path

import pytest

from agentctl.errors import ConfigError
from agentctl.storage import SCHEMA_VERSION, open_connection, open_database


def test_newer_registry_is_rejected_without_writes(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version', '5');
        CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT);
        INSERT INTO messages VALUES(1, 'future data');
        """
    )
    before = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()

    with pytest.raises(ConfigError, match="newer than this agentctl supports"):
        open_connection(path)

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT body FROM messages WHERE id = 1").fetchone()[0] == "future data"
    after = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    assert after == before
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == journal_mode
    conn.close()


def test_legacy_registry_with_pending_inbox_upgrades(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with open_database(path) as conn:
        conn.executescript("""
            UPDATE meta SET value = '3' WHERE key = 'schema_version';
            INSERT INTO workspaces VALUES ('ws_one', '/example', 'leader_one', NULL, 't', 't');
            CREATE TABLE events (id TEXT PRIMARY KEY);
            CREATE TABLE inbox (event_id TEXT REFERENCES events(id));
            INSERT INTO events VALUES ('event_one');
            INSERT INTO inbox VALUES ('event_one');
        """)

    with open_database(path) as conn:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        assert version[0] == str(SCHEMA_VERSION)
        assert conn.execute("SELECT id FROM workspaces").fetchone()[0] == "ws_one"
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name IN ('events', 'inbox')").fetchall()
