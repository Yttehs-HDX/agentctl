"""Workspace discovery: which workspace does this command belong to?

Precedence, highest first:

1. an explicit `--workspace` path,
2. `AGENTCTL_WORKSPACE`,
3. the registered workspace whose root is the current directory,
4. the nearest registered workspace above the current directory.

Two rules keep discovery from surprising anyone:

- It never creates a workspace and never binds one loosely. Discovery only finds
  a root that `init` already registered, and the nearest (deepest) match wins so
  nested workspaces resolve to the innermost one.
- `init` is the one command that may name an unregistered directory: without an
  explicit path it uses the current directory, which is how a project gets
  registered in the first place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agentctl.errors import InvalidPath, WorkspaceNotInitialized
from agentctl.storage.models import Workspace
from agentctl.workspace.resolver import canonical


@dataclass(slots=True)
class WorkspaceChoice:
    """A resolved workspace plus how it was found, for `info`/`--json` output."""

    workspace: Workspace
    source: str  # "flag" | "env" | "cwd" | "parent"

    def to_dict(self) -> dict[str, object]:
        return {"source": self.source, **self.workspace.to_dict()}


def _ancestors(path: str) -> list[str]:
    """`path` and each of its parents, deepest first, as canonical strings."""
    current = Path(path)
    chain: list[str] = []
    for candidate in [current, *current.parents]:
        chain.append(str(candidate))
    return chain


def explicit_root(value: str | None, *, allow_missing: bool = False) -> str | None:
    """Canonicalize a caller-supplied path, or None when none was given."""
    if value is None or value == "":
        return None
    return canonical(value, allow_missing=allow_missing)


def nearest_workspace(store, path: str) -> Workspace | None:
    """The deepest registered workspace root at or above `path`.

    Comparison is by path component (see `is_inside`), so `/srv/app` never
    claims `/srv/app-two`. Reading every workspace once keeps this to a single
    query even when the caller is many directories deep.
    """
    candidates = [w for w in store.workspaces.list_all() if _contains(w.root_path, path)]
    if not candidates:
        return None
    return max(candidates, key=lambda w: len(Path(w.root_path).parts))


def _contains(root: str, path: str) -> bool:
    root_path = Path(root)
    candidate = Path(path)
    return candidate == root_path or root_path in candidate.parents


def discover(
    store,
    *,
    explicit: str | None = None,
    env: str | None = None,
    cwd: str | None = None,
    allow_missing: bool = False,
) -> WorkspaceChoice:
    """Resolve the workspace for a command that requires an existing one."""
    resolved = explicit_root(explicit, allow_missing=allow_missing)
    if resolved is not None:
        workspace = store.workspaces.find_by_root(resolved)
        if workspace is None:
            raise WorkspaceNotInitialized(
                f"workspace {resolved} is not initialized",
                'run `agentctl init` in that directory once',
            )
        return WorkspaceChoice(workspace, "flag")

    resolved_env = explicit_root(env, allow_missing=allow_missing)
    if resolved_env is not None:
        workspace = store.workspaces.find_by_root(resolved_env)
        if workspace is None:
            raise WorkspaceNotInitialized(
                f"AGENTCTL_WORKSPACE points at {resolved_env}, which is not initialized",
                "run `agentctl init` there, or unset AGENTCTL_WORKSPACE",
            )
        return WorkspaceChoice(workspace, "env")

    here = canonical(cwd or os.getcwd())
    found = nearest_workspace(store, here)
    if found is None:
        raise WorkspaceNotInitialized(
            f"no registered workspace at or above {here}",
            "run `agentctl init` in the project root, or pass --workspace PATH",
        )
    return WorkspaceChoice(found, "cwd" if found.root_path == here else "parent")


def init_target(*, explicit: str | None = None, env: str | None = None, cwd: str | None = None) -> tuple[str, str]:
    """The root `init` should register, plus where that root came from."""
    resolved = explicit_root(explicit)
    if resolved is not None:
        return resolved, "flag"
    resolved_env = explicit_root(env)
    if resolved_env is not None:
        return resolved_env, "env"
    return canonical(cwd or os.getcwd()), "cwd"


def ancestors_for_error(path: str) -> list[str]:
    """Parent directories, used in the "did you mean" hint."""
    return _ancestors(path)[1:6]


def describe_missing(path: str, store) -> str:
    known = [w.root_path for w in store.workspaces.list_all()]
    if not known:
        return "no workspaces are registered on this machine yet"
    return f"registered workspaces: {', '.join(sorted(known)[:5])}"


__all__ = [
    "InvalidPath",
    "WorkspaceChoice",
    "ancestors_for_error",
    "describe_missing",
    "discover",
    "explicit_root",
    "init_target",
    "nearest_workspace",
]
