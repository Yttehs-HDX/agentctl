"""Spool: how a harness hook reports a fact without spawning a process.

Why a file instead of a command
-------------------------------
Measured on OMP 19.1.0: `pi.exec` fails with `ENOENT ... posix_spawn` when it is
called from an `input` handler. That is the handler which sees an incoming
message, so a hook that reported through a subprocess could never acknowledge
one. Node built-ins are available to an extension, so the hook appends one JSON
line to a spool file instead: microseconds, no process, nothing to fail.

The spool is intentionally dumb. A record says what the worker observed; it
carries no authority. Ingest binds it to a message row, refuses anything that
does not belong to the workspace, and is idempotent, so a replayed or duplicated
line changes nothing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from agentctl.clock import now_precise
from agentctl.integrations.hooks import (
    record_message_received,
    record_session_started,
    record_turn_completed,
    record_turn_failed,
)
from agentctl.paths import spool_dir

KINDS = ("session_started", "message_received", "turn_completed", "turn_failed")


def spool_path(agent: str) -> Path:
    """One spool per harness. Records carry their workspace and session ids."""
    return spool_dir() / f"{agent}.jsonl"


def spool_paths() -> list[Path]:
    directory = spool_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.jsonl"))


@dataclass(slots=True)
class DrainReport:
    applied: list[dict[str, object]] = field(default_factory=list)
    rejected: list[dict[str, object]] = field(default_factory=list)
    records: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "records": self.records,
            "applied": len(self.applied),
            "rejected": len(self.rejected),
            "rejections": self.rejected[:20],
        }


def _read_lines(path: Path) -> tuple[list[str], int]:
    """Read and atomically claim the spool.

    Renaming before reading means a hook appending concurrently writes to a new
    file, so no line is ever torn between two readers. Two drains racing each
    other produce one winner per rename and never lose a record.
    """
    if not path.exists():
        return [], 0
    claimed = path.with_name(f"{path.name}.{os.getpid()}.{now_precise().replace(':', '')}")
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return [], 0
    try:
        text = claimed.read_text(encoding="utf-8", errors="replace")
    finally:
        # Never lose a record to a read error: keep the claimed file aside.
        try:
            claimed.unlink()
        except OSError:
            pass
    return [line for line in text.splitlines() if line.strip()], len(text)


def drain(store, workspace, *, limit: int = 5000) -> DrainReport:
    """Apply every spooled record addressed to this workspace.

    Records from other workspaces are skipped without being lost: they stay for
    the drain of the workspace they belong to only if that workspace is drained
    from the same file, so the file is filtered, never truncated by ownership.
    """
    report = DrainReport()
    for path in spool_paths():
        report.records += _drain_file(store, workspace, path, limit=limit, report=report)
    return report


def _drain_file(store, workspace, path: Path, *, limit: int, report: DrainReport) -> int:
    lines, _size = _read_lines(path)
    for line in lines[:limit]:
        report.records += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            report.rejected.append({"reason": "malformed line", "line": line[:200]})
            continue
        if not isinstance(record, dict):
            report.rejected.append({"reason": "not an object", "line": line[:200]})
            continue
        kind = record.get("kind")
        if kind not in KINDS:
            report.rejected.append({"reason": f"unknown kind {kind!r}", "record": record})
            continue
        if record.get("workspace_id") not in (None, workspace.id):
            # A hook from another workspace must never write into this one.
            report.rejected.append({"reason": "workspace mismatch", "record": record})
            continue
        try:
            outcome = _apply(store, workspace, kind, record)
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the drain
            report.rejected.append({"reason": str(exc), "record": record})
            continue
        report.applied.append({"kind": kind, **outcome})
    return len(lines[:limit])


def _apply(store, workspace, kind: str, record: dict) -> dict[str, object]:
    session_id = record.get("session_id")
    if kind == "session_started":
        return record_session_started(store, workspace, session_id, agent=record.get("agent"))
    if kind == "message_received":
        return record_message_received(
            store,
            workspace,
            session_id,
            message_id=record.get("message_id"),
            source=record.get("agent") or "hook",
        )
    if kind == "turn_completed":
        return record_turn_completed(
            store,
            workspace,
            session_id,
            message_id=record.get("message_id"),
            source=record.get("agent") or "hook",
            result=record.get("result"),
        )
    if kind == "turn_failed":
        return record_turn_failed(
            store,
            workspace,
            session_id,
            message_id=record.get("message_id"),
            source=record.get("agent") or "hook",
            reason=record.get("reason"),
        )
    raise ValueError(f"unknown kind {kind!r}")
