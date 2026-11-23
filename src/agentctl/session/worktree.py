"""Git worktree management for `--mode worktree` sessions.

Worktrees live under `<workspace-root>/.agentctl/worktrees/<session-id>` so they
stay inside the workspace boundary and never collide with the project tree.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from agentctl.errors import AgentctlError, InvalidPath

WORKTREE_DIR_NAME = ".agentctl"


class WorktreeError(AgentctlError):
    code = "worktree_error"
    exit_code = 6


def git_toplevel(path: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def worktree_root(workspace_root: str) -> Path:
    return Path(workspace_root) / WORKTREE_DIR_NAME / "worktrees"


def create_worktree(
    *,
    workspace_root: str,
    repo_root: str,
    session_name: str,
    session_id: str,
    base_ref: str | None = None,
) -> tuple[str, str]:
    """Create a worktree; returns (path, branch)."""
    parent = worktree_root(workspace_root)
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / session_id
    branch = f"agentctl/{_branch_slug(session_name)}"
    args = ["git", "-C", repo_root, "worktree", "add", "-b", branch, str(target)]
    if base_ref:
        args.append(base_ref)
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise WorktreeError(
            f"git worktree add failed: {proc.stderr.strip() or proc.stdout.strip()}",
            f"check that {repo_root} is a clean git repository",
        )
    _exclude_worktree_dir(repo_root)
    return str(target), branch


def worktree_is_clean(path: str) -> bool:
    """True when the worktree has no modified, staged or untracked files."""
    proc = subprocess.run(
        ["git", "-C", path, "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False
    return not proc.stdout.strip()


def _branch_slug(name: str) -> str:
    """Session names are free-form; git refs are not."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return slug or "session"


def remove_worktree(*, repo_root: str, path: str, branch: str | None) -> None:
    """Remove a clean worktree. The branch is kept so worker commits survive."""
    subprocess.run(
        ["git", "-C", repo_root, "worktree", "remove", "--force", path],
        capture_output=True,
        text=True,
    )
    if branch:
        # Remove the branch only when git can do it without losing commits
        # (i.e. it is fully merged). -d refuses otherwise; -D would destroy work.
        subprocess.run(
            ["git", "-C", repo_root, "branch", "-d", branch],
            capture_output=True,
            text=True,
        )


def _exclude_worktree_dir(repo_root: str) -> None:
    """Keep .agentctl/ out of `git status` in the main checkout."""
    proc = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return
    common = Path(proc.stdout.strip())
    if not common.is_absolute():
        common = Path(repo_root) / common
    exclude = common / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        entry = f"{WORKTREE_DIR_NAME}/"
        if entry not in existing.splitlines():
            with exclude.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(entry + "\n")
    except OSError:
        pass


def ensure_git_repo(path: str) -> str:
    root = git_toplevel(path)
    if root is None:
        raise InvalidPath(
            f"{path} is not inside a git repository",
            "use --mode shared, or initialise git first",
        )
    return root
