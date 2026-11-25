"""Install the bundled Leader skill into agent-harness skill directories.

The skill lives in the checkout at `skills/agentctl-leader/` and is shipped
inside the wheel as `agentctl/skills/agentctl-leader/`. One directory per
(harness, scope) target, linked by default so an edit in the source is visible
without reinstalling.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import agentctl
from agentctl.errors import SkillSourceMissing, SkillTargetExists

SKILL_ID = "agentctl-leader"
SKILL_FILE = "SKILL.md"

SCOPES = ("user", "project")
HARNESSES = ("omp", "claude", "agents")

Target = tuple[str, str, str]


@dataclass(slots=True)
class InstallResult:
    harness: str
    scope: str
    path: str
    action: str
    error: str | None = None

    def to_dict(self) -> dict[str, str]:
        body = {"harness": self.harness, "scope": self.scope, "path": self.path, "action": self.action}
        if self.error:
            body["error"] = self.error
        return body


@dataclass(slots=True)
class StatusResult:
    harness: str
    scope: str
    path: str
    state: str

    def to_dict(self) -> dict[str, str]:
        return {"harness": self.harness, "scope": self.scope, "path": self.path, "state": self.state}


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


def target_dirs(
    scope: str,
    cwd: Path,
    home: Path,
    env: Mapping[str, str],
) -> list[Target]:
    """Every target for `scope`, in harness order.

    The neutral `agents` layout is project-only, so `scope="user"` omits it.
    """
    if scope == "project":
        return [
            ("omp", scope, str(cwd / ".omp" / "skills" / SKILL_ID)),
            ("claude", scope, str(cwd / ".claude" / "skills" / SKILL_ID)),
            ("agents", scope, str(cwd / ".agents" / "skills" / SKILL_ID)),
        ]
    if scope == "user":
        profile = env.get("OMP_PROFILE")
        if profile:
            omp = home / ".omp" / "profiles" / profile / "agent" / "skills" / SKILL_ID
        else:
            omp = home / ".omp" / "agent" / "skills" / SKILL_ID
        return [
            ("omp", scope, str(omp)),
            ("claude", scope, str(home / ".claude" / "skills" / SKILL_ID)),
        ]
    raise ValueError(f"unknown scope {scope!r}; expected one of {', '.join(SCOPES)}")


def install(
    targets: Sequence[Target],
    *,
    copy: bool = False,
    force: bool = False,
) -> list[InstallResult]:
    """Create or replace each target. A refusal does not stop the others."""
    source = source_dir()
    results: list[InstallResult] = []
    for harness, scope, raw_path in targets:
        path = Path(raw_path)
        try:
            action = _install_one(path, source, copy=copy, force=force)
            results.append(InstallResult(harness, scope, str(path), action))
        except SkillTargetExists as exc:
            results.append(InstallResult(harness, scope, str(path), "skipped", exc.message))
    return results


def status(targets: Sequence[Target]) -> list[StatusResult]:
    source = source_dir()
    return [StatusResult(harness, scope, path, _classify(Path(path), source)) for harness, scope, path in targets]


def uninstall(targets: Sequence[Target]) -> list[InstallResult]:
    """Remove only entries this module would have created."""
    source = source_dir()
    results: list[InstallResult] = []
    for harness, scope, raw_path in targets:
        path = Path(raw_path)
        try:
            action = _uninstall_one(path, source)
            results.append(InstallResult(harness, scope, str(path), action))
        except SkillTargetExists as exc:
            results.append(InstallResult(harness, scope, str(path), "skipped", exc.message))
    return results


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


def _classify(path: Path, source: Path) -> str:
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
    """A directory whose only entry is a SKILL.md identical to the source's."""
    try:
        entries = sorted(entry.name for entry in path.iterdir())
    except OSError:
        return False
    if entries != [SKILL_FILE]:
        return False
    try:
        return (path / SKILL_FILE).read_bytes() == (source / SKILL_FILE).read_bytes()
    except OSError:
        return False
