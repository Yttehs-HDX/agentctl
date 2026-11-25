"""Install the bundled Leader skill into the user or project skill directory.

The skill lives in the checkout at `skills/agentctl-leader/` and is shipped
inside the wheel as `agentctl/skills/agentctl-leader/`. The target is linked to
the source by default.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import agentctl
from agentctl.errors import SkillSourceMissing, SkillTargetExists

SKILL_ID = "agentctl-leader"
SKILL_FILE = "SKILL.md"


@dataclass(slots=True)
class InstallResult:
    action: str
    error: str | None = None


def source_dir() -> Path:
    """The skill directory, preferring the packaged copy over the checkout."""
    package_dir = Path(agentctl.__file__).parent
    packaged = package_dir / "skills" / SKILL_ID
    checkout = package_dir.parent.parent / "skills" / SKILL_ID
    for candidate in (packaged, checkout):
        if (candidate / SKILL_FILE).is_file():
            return candidate.resolve()
    raise SkillSourceMissing(
        f"skill {SKILL_ID} not found; looked in {packaged} and {checkout}",
        "reinstall agentctl, or run from a checkout that contains skills/agentctl-leader",
    )


def install(
    path: Path,
    source: Path,
    *,
    copy: bool = False,
    force: bool = False,
) -> InstallResult:
    """Create or replace the target."""
    try:
        return InstallResult(_install_one(path, source, copy=copy, force=force))
    except SkillTargetExists as exc:
        return InstallResult("skipped", exc.message)


def uninstall(path: Path, source: Path) -> InstallResult:
    """Remove only an entry this module would have created."""
    try:
        return InstallResult(_uninstall_one(path, source))
    except SkillTargetExists as exc:
        return InstallResult("skipped", exc.message)


def _install_one(path: Path, source: Path, *, copy: bool, force: bool) -> str:
    if not path.exists() and not path.is_symlink():
        _create(path, source, copy=copy)
        return "created"
    if path.is_symlink() and _same_link(path, source):
        return "unchanged"
    if not force:
        raise SkillTargetExists(
            f"{path} already exists and was not created by agentctl skills install",
            "rerun with --force to replace it",
        )
    _remove(path)
    _create(path, source, copy=copy)
    return "replaced"


def _uninstall_one(path: Path, source: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "unchanged"
    if path.is_symlink() and _inside_source(path, source):
        path.unlink()
        return "removed"
    if path.is_dir() and _is_our_copy(path, source):
        shutil.rmtree(path)
        return "removed"
    raise SkillTargetExists(
        f"{path} was not created by agentctl skills install, leaving it alone",
        "remove it yourself if it should go",
    )


def status(path: Path, source: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "missing"
    if path.is_symlink():
        return "linked" if _same_link(path, source) else "foreign"
    if path.is_dir() and _is_our_copy(path, source):
        return "copied"
    return "foreign"


def _create(path: Path, source: Path, *, copy: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if copy or os.name == "nt":
        shutil.copytree(source, path)
    else:
        path.symlink_to(source, target_is_directory=True)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _same_link(path: Path, source: Path) -> bool:
    try:
        return path.resolve() == source
    except OSError:
        return False


def _inside_source(path: Path, source: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == source or source in resolved.parents


def _is_our_copy(path: Path, source: Path) -> bool:
    """Recognize a copy only when its entire tree matches the source."""
    try:
        return _same_tree(path, source)
    except OSError:
        return False


def _same_tree(path: Path, source: Path) -> bool:
    if path.is_symlink() or source.is_symlink():
        return False
    if source.is_dir():
        if not path.is_dir():
            return False
        entries = {entry.name for entry in source.iterdir()}
        return entries == {entry.name for entry in path.iterdir()} and all(
            _same_tree(path / name, source / name) for name in entries
        )
    return source.is_file() and path.is_file() and path.read_bytes() == source.read_bytes()
