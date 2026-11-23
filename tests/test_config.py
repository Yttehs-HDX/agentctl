"""Configuration: defaults, TOML round-trip, validation, setup semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentctl.config import default_config, load_config, save_config
from agentctl.config.loader import dumps
from agentctl.config.schema import Config
from agentctl.errors import ConfigError


def test_default_config_has_all_catalog_agents() -> None:
    config = default_config()
    assert set(config.agents) >= {"omp", "codex", "claude", "pi", "opencode"}
    assert config.defaults.agent == ""
    assert config.session.default_mode == "shared"


def test_save_then_load_round_trips(isolated_home: Path) -> None:
    config = default_config()
    config.defaults.agent = "codex"
    config.agents["pi"].enabled = False
    path = save_config(config)
    assert path == isolated_home / "config.toml"
    loaded = load_config()
    assert loaded.defaults.agent == "codex"
    assert loaded.agents["pi"].enabled is False
    assert loaded.agents["pi"].command == ["pi"]


def test_dumped_toml_is_parseable() -> None:
    import tomllib

    text = dumps(default_config())
    parsed = tomllib.loads(text)
    assert parsed["general"]["schema_version"] == 1
    assert parsed["defaults"]["agent"] == ""
    assert parsed["agents"]["omp"]["command"] == ["omp"]


def test_default_agent_may_be_empty(isolated_home: Path) -> None:
    save_config(default_config())
    assert load_config().defaults.agent == ""


def test_unknown_top_level_key_is_rejected(isolated_home: Path) -> None:
    path = isolated_home / "config.toml"
    path.write_text("[general]\nschema_version = 1\n\n[bogus]\nx = 1\n")
    with pytest.raises(ConfigError):
        load_config()


def test_wrong_schema_version_is_rejected(isolated_home: Path) -> None:
    path = isolated_home / "config.toml"
    path.write_text("[general]\nschema_version = 99\n")
    with pytest.raises(ConfigError):
        load_config()


def test_broken_toml_is_rejected(isolated_home: Path) -> None:
    (isolated_home / "config.toml").write_text("[general\nschema_version=1")
    with pytest.raises(ConfigError):
        load_config()


def test_agent_command_must_not_be_empty(isolated_home: Path) -> None:
    path = isolated_home / "config.toml"
    path.write_text('[general]\nschema_version = 1\n\n[agents.omp]\ncommand = []\n')
    with pytest.raises(ConfigError):
        load_config()


def test_dump_preserves_disabled_agents(isolated_home: Path) -> None:
    config = default_config()
    config.agents["opencode"].enabled = False
    saved = save_config(config)
    text = saved.read_text()
    assert "[agents.omp]" in text
    assert "enabled = true" in text
    assert load_config().agents["opencode"].enabled is False
