"""Filesystem locations of agentctl's own state.

On POSIX everything lives in one directory so a profile can be relocated with
AGENTCTL_HOME (then XDG_CONFIG_HOME, then ~/.config). On Windows config and
state are separate roots: %APPDATA% holds config.toml, %LOCALAPPDATA% holds the
registry and per-session directories. AGENTCTL_HOME overrides both and must be
absolute, so tests and alternate profiles can still point at one directory.
"""

from __future__ import annotations

import ntpath
import os
from collections.abc import Mapping
from pathlib import Path

from agentctl.errors import InvalidPath


def _absolute(value: str | None, os_name: str) -> Path | None:
    """`value` as a Path when it is absolute for `os_name`, else None.

    Comparison uses `ntpath` for the Windows branch so a `C:/...` value is
    judged by Windows' rules even while the tests run on POSIX.
    """
    if not value:
        return None
    if os_name == "nt":
        return Path(value) if ntpath.isabs(value) else None
    path = Path(value)
    return path if path.is_absolute() else None


def _agentctl_home(os_name: str, env: Mapping[str, str]) -> Path | None:
    override = env.get("AGENTCTL_HOME")
    if not override:
        return None
    path = Path(override).expanduser()
    if _absolute(str(path), os_name) is None:
        raise InvalidPath(
            f"AGENTCTL_HOME must be an absolute path, got {override!r}",
            "set AGENTCTL_HOME to an absolute path, or unset it",
        )
    return path


def _config_dir(os_name: str, env: Mapping[str, str], home: Path) -> Path:
    override = _agentctl_home(os_name, env)
    if override is not None:
        return override
    if os_name == "nt":
        base = _absolute(env.get("APPDATA"), os_name) or home / "AppData" / "Roaming"
        return base / "agentctl"
    base = _absolute(env.get("XDG_CONFIG_HOME"), os_name) or home / ".config"
    return base / "agentctl"


def _data_dir(os_name: str, env: Mapping[str, str], home: Path) -> Path:
    override = _agentctl_home(os_name, env)
    if override is not None:
        return override
    if os_name == "nt":
        base = _absolute(env.get("LOCALAPPDATA"), os_name) or home / "AppData" / "Local"
        return base / "agentctl"
    # One directory on POSIX: existing users keep their registry and sessions.
    return _config_dir(os_name, env, home)


def config_dir() -> Path:
    return _config_dir(os.name, os.environ, Path.home())


def data_dir() -> Path:
    return _data_dir(os.name, os.environ, Path.home())


def config_path() -> Path:
    return config_dir() / "config.toml"


def database_path() -> Path:
    return data_dir() / "registry.db"


def session_state_dir() -> Path:
    return data_dir() / "sessions"
