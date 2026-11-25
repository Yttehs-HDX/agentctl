"""State-directory resolution: AGENTCTL_HOME, XDG and the Windows split."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentctl.errors import InvalidPath
from agentctl.paths import (
    _config_dir,
    _data_dir,
    config_dir,
    config_path,
    data_dir,
    database_path,
    session_state_dir,
)


def test_agentctl_home_wins_over_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "profile"
    monkeypatch.setenv("AGENTCTL_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_dir() == home
    assert data_dir() == home
    assert config_path() == home / "config.toml"
    assert database_path() == home / "registry.db"
    assert session_state_dir() == home / "sessions"


def test_agentctl_home_expands_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTCTL_HOME", "~/custom-agentctl")
    assert config_dir() == tmp_path / "custom-agentctl"


def test_relative_agentctl_home_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTCTL_HOME", "relative-agentctl-home")
    with pytest.raises(InvalidPath) as caught:
        config_dir()
    assert "relative-agentctl-home" in caught.value.message


def test_absolute_xdg_config_home_gets_an_agentctl_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTCTL_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_dir() == tmp_path / "xdg" / "agentctl"
    assert database_path() == tmp_path / "xdg" / "agentctl" / "registry.db"


@pytest.mark.parametrize("xdg", ["relative/xdg", ""])
def test_relative_or_empty_xdg_falls_back_to_dot_config(
    xdg: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTCTL_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    assert config_dir() == tmp_path / ".config" / "agentctl"


def test_windows_config_dir_uses_appdata(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "x"
    env = {"APPDATA": "C:/Users/x/AppData/Roaming"}
    assert _config_dir("nt", env, home) == Path("C:/Users/x/AppData/Roaming") / "agentctl"


def test_windows_config_dir_falls_back_without_appdata(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "x"
    expected = home / "AppData" / "Roaming" / "agentctl"
    assert _config_dir("nt", {}, home) == expected
    assert _config_dir("nt", {"APPDATA": "relative/appdata"}, home) == expected


def test_windows_data_dir_uses_localappdata(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "x"
    env = {"LOCALAPPDATA": "C:/Users/x/AppData/Local"}
    assert _data_dir("nt", env, home) == Path("C:/Users/x/AppData/Local") / "agentctl"
    assert _data_dir("nt", {}, home) == home / "AppData" / "Local" / "agentctl"
    # AGENTCTL_HOME collapses the split back into one directory.
    assert _data_dir("nt", {"AGENTCTL_HOME": "C:/agentctl"}, home) == Path("C:/agentctl")
    assert _config_dir("nt", {"AGENTCTL_HOME": "C:/agentctl"}, home) == Path("C:/agentctl")


def test_posix_data_dir_is_the_config_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert _data_dir("posix", {}, home) == _config_dir("posix", {}, home)
