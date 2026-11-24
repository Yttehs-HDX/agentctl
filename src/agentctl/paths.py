"""Filesystem locations of agentctl's own state.

Everything lives in one directory so tests and alternate profiles can relocate
it with AGENTCTL_HOME (then XDG_CONFIG_HOME, then ~/.config).
"""

from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    override = os.environ.get("AGENTCTL_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "agentctl"


def config_path() -> Path:
    return config_dir() / "config.toml"


def database_path() -> Path:
    return config_dir() / "registry.db"


def session_state_dir() -> Path:
    return config_dir() / "sessions"


def bin_dir() -> Path:
    """Directory holding the launcher the hooks call.

    Hooks run inside somebody else's agent, whose PATH agentctl does not
    control. A launcher with an absolute location is the only reliable handle,
    so `integrations install` writes one here and every hook uses it.
    """
    return config_dir() / "bin"


def launcher_path() -> Path:
    return bin_dir() / "agentctl"


def integrations_dir() -> Path:
    """Where agentctl keeps the hook assets it hands to harnesses."""
    return config_dir() / "integrations"
