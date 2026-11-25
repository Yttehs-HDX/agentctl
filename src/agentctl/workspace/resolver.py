"""Workspace identity: canonical paths in, stable UUIDs out.

A Workspace is found by its canonical root path. Its id and leader id are
created once and never derived from the path, the pid or a lock, so a Leader
that restarts in the same directory gets its old identity back.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agentctl.errors import InvalidPath, WorkspaceNotInitialized
from agentctl.storage.models import Workspace


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(path: str | os.PathLike[str], *, allow_missing: bool = False) -> str:
    """Return an absolute, symlink-resolved directory path."""
    raw = Path(path).expanduser()
    try:
        resolved = Path(os.path.realpath(raw, strict=not allow_missing))
    except FileNotFoundError as exc:
        raise InvalidPath(f"{raw} does not exist") from exc
    except OSError as exc:
        raise InvalidPath(f"{raw} cannot be resolved: {exc}") from exc
    if not resolved.is_dir() and (not allow_missing or resolved.exists()):
        raise InvalidPath(f"{resolved} is not a directory")
    return str(resolved)


def resolve_root(workspace_path: str | None) -> str:
    if workspace_path is None or workspace_path == "":
        raise InvalidPath(
            "--workspace is required",
            'pass the Leader workspace root, e.g. --workspace="$(pwd)"',
        )
    return canonical(workspace_path)


def is_inside(child: str, parent: str) -> bool:
    """True when `child` is `parent` or lives below it (both canonical)."""
    child_path = Path(child)
    parent_path = Path(parent)
    return child_path == parent_path or parent_path in child_path.parents


def ensure_inside_workspace(path: str, root: str) -> None:
    if not is_inside(path, root):
        raise InvalidPath(
            f"{path} is outside the workspace root {root}",
            "sessions may only work inside their workspace in v0.1",
        )


def new_workspace(root: str, default_agent: str | None = None) -> Workspace:
    stamp = now()
    return Workspace(
        id=f"ws_{uuid.uuid4().hex}",
        root_path=root,
        leader_id=f"leader_{uuid.uuid4().hex}",
        default_agent=default_agent,
        created_at=stamp,
        updated_at=stamp,
    )


class WorkspaceResolver:
    """Look up or create the workspace record for a canonical root."""

    def __init__(self, store) -> None:
        self.store = store

    def find(self, root: str) -> Workspace | None:
        return self.store.workspaces.find_by_root(root)

    def require(self, root: str) -> Workspace:
        workspace = self.find(root)
        if workspace is None:
            raise WorkspaceNotInitialized(
                f"workspace {root} is not initialized",
                'run `agentctl --workspace="$(pwd)" init` once for this directory',
            )
        return workspace

    def get_or_create(self, root: str, default_agent: str | None = None) -> tuple[Workspace, bool]:
        existing = self.find(root)
        if existing is not None:
            return existing, False
        workspace = new_workspace(root, default_agent)
        self.store.workspaces.insert(workspace)
        return workspace, True
