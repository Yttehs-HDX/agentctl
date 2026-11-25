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


def repair_worktree(repo_root: str, path: str) -> None:
    """Repair Git metadata after a repository or linked worktree moves."""
    proc = subprocess.run(
        ["git", "-C", repo_root, "worktree", "repair", path],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        message = detail[-1] if detail else f"git exited {proc.returncode}"
        raise WorktreeError(
            f"git worktree repair failed: {message}",
            f"inspect `git -C {repo_root} worktree list` and the worktree at {path}",
        )


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
    # The session suffix keeps the branch unique per repository: a name can be
    # reused after a delete, and a kept worktree can keep its branch alive.
    suffix = session_id.removeprefix("sess_")[:8]
    branch = f"agentctl/{_branch_slug(session_name)[:48]}-{suffix}"
    if not _valid_branch(branch):
        raise WorktreeError(
            f"cannot derive a valid git branch from session name {session_name!r}",
            "rename the session using letters, digits, '.', '_' or '-'",
        )
    args = ["git", "-C", repo_root, "worktree", "add", "-b", branch, str(target)]
    if base_ref:
        args.append(base_ref)
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        message = detail[-1] if detail else f"git exited {proc.returncode}"
        raise WorktreeError(
            f"git worktree add failed: {message}",
            f"inspect `git -C {repo_root} worktree list` and `git -C {repo_root} branch --list`",
        )
    _exclude_worktree_dir(repo_root)
    return str(target), branch


def _valid_branch(branch: str) -> bool:
    proc = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


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
    """Session names are free-form; git refs are not.

    Rules enforced (git-check-ref-format):
    - no `..`, no `@{`, no leading/trailing `/` or `.`
    - no component ending in `.lock`, no control chars, no space `~^:?*[\\`
    - each `/`-separated component must not start with `.`
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    slug = re.sub(r"\.{2,}", ".", slug)  # `a..b` is rejected by git
    slug = re.sub(r"@\{", "-", slug)
    slug = slug.strip("-./")
    if slug.endswith(".lock"):
        slug = slug[: -len(".lock")].rstrip("-.")
    if not slug:
        return "session"
    return slug


def remove_worktree(*, repo_root: str, path: str, branch: str | None) -> tuple[bool, str | None]:
    """Remove a clean worktree, reporting whether git actually did it.

    The branch is dropped only when git considers it merged (`-d` refuses
    otherwise), so that refusal is expected and is not a failure: worker commits
    stay. The worktree removal itself must be reported, because the caller marks
    the session deleted and may not claim cleanup it did not get.
    """
    proc = subprocess.run(
        ["git", "-C", repo_root, "worktree", "remove", "--force", path],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return False, detail[-1] if detail else f"git worktree remove exited {proc.returncode}"
    if branch:
        # Remove the branch only when git can do it without losing commits
        # (i.e. it is fully merged). -d refuses otherwise; -D would destroy work.
        subprocess.run(
            ["git", "-C", repo_root, "branch", "-d", branch],
            capture_output=True,
            text=True,
        )
    return True, None


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
