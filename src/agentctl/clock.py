"""Clock and identity helpers shared by every layer."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone


def now() -> str:
    """UTC timestamp with second resolution; the registry's stored form."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_precise() -> str:
    """UTC timestamp with microsecond resolution, for lease bookkeeping."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        value = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def plus(stamp: str, seconds: float) -> str:
    base = parse(stamp) or datetime.now(timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def elapsed_since(stamp: str | None) -> float | None:
    """Seconds between `stamp` and now, or None when it cannot be parsed."""
    base = parse(stamp)
    if base is None:
        return None
    return (datetime.now(timezone.utc) - base).total_seconds()


def token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"
