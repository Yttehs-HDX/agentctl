"""One timestamp format, so stored rows sort and compare."""

from __future__ import annotations

from datetime import datetime, timezone


def now() -> str:
    """UTC timestamp with second resolution; the registry's stored form."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
