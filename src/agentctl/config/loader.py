"""Read and write config.toml.

Python 3.11+ ships tomllib for reading; writes go through a tiny serializer so
the file keeps a stable, human-editable shape with one table per section.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from agentctl.config.schema import (
    SCHEMA_VERSION,
    AgentEntry,
    Config,
)
from agentctl.errors import ConfigError
from agentctl.paths import config_path

# Agents shipped with agentctl. Anything absent from config.toml is not part of
# the catalog: the Leader cannot start programs the user never configured.
DEFAULT_AGENTS: dict[str, list[str]] = {
    "omp": ["omp"],
    "codex": ["codex"],
    "claude": ["claude"],
    "opencode": ["opencode"],
    "pi": ["pi"],
}


def default_config() -> Config:
    return Config(
        agents={name: AgentEntry(command=list(cmd), enabled=True) for name, cmd in DEFAULT_AGENTS.items()}
    )


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        raise FileNotFoundError(str(path))
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}", "fix or delete the file, then run `agentctl setup`") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        config = Config.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"{path} does not match the config schema: {exc}", "fix the file or run `agentctl setup`") from exc
    if config.general.schema_version != SCHEMA_VERSION:
        raise ConfigError(
            f"config schema_version {config.general.schema_version} is not supported (expected {SCHEMA_VERSION})",
            "run `agentctl setup` to rewrite the configuration",
        )
    return config


def load_or_default() -> Config:
    """Config if it exists, otherwise the built-in defaults (no write)."""
    try:
        return load_config()
    except FileNotFoundError:
        return default_config()


def save_config(config: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dumps(config)
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def dumps(config: Config) -> str:
    lines: list[str] = []
    lines.append("[general]")
    lines.append(f"schema_version = {config.general.schema_version}")
    lines.append("")
    lines.append("[defaults]")
    lines.append(f"agent = {_toml_str(config.defaults.agent)}")
    lines.append("")
    lines.append("[runtime]")
    lines.append(f"backend = {_toml_str(config.runtime.backend)}")
    lines.append(f"hc_command = {_toml_str(config.runtime.hc_command)}")
    if config.runtime.call_timeout_seconds:
        lines.append(f"call_timeout_seconds = {config.runtime.call_timeout_seconds:g}")
    lines.append("")
    lines.append("[workspace]")
    lines.append(f"path_policy = {_toml_str(config.workspace.path_policy)}")
    lines.append("")
    lines.append("[session]")
    lines.append(f"default_mode = {_toml_str(config.session.default_mode)}")
    lines.append("")
    for name in sorted(config.agents):
        entry = config.agents[name]
        lines.append(f"[agents.{_toml_key(name)}]")
        lines.append(f"command = [{', '.join(_toml_str(part) for part in entry.command)}]")
        lines.append(f"enabled = {_toml_bool(entry.enabled)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_str(value: str) -> str:
    out = ['"']
    for char in value:
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _toml_key(value: str) -> str:
    if value and all(ch.isalnum() or ch in "-_" for ch in value):
        return value
    return _toml_str(value)


def config_as_dict(config: Config) -> dict[str, Any]:
    return config.model_dump(mode="python")
