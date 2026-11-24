"""Integrations: capability honesty, idempotent install, no user-config edits."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentctl.errors import AgentctlError
from agentctl.integrations.registry import ADAPTERS, describe_all, install, uninstall
from agentctl.storage import Store, open_connection
from agentctl.workspace.resolver import WorkspaceResolver


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTCTL_HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    (tmp_path / "userhome").mkdir()
    conn = open_connection(home / "registry.db")
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(tmp_path / "proj"))
    yield store, ws, home
    conn.close()


def test_capabilities_are_honest_about_claude_turn_start(env) -> None:
    _store, _ws, _home = env
    caps = ADAPTERS["claude"].capabilities()
    assert caps.receive_message is True
    assert caps.turn_completed is True
    assert caps.turn_started is False, "no Claude hook fires at turn start"
    assert "turn_started" in caps.missing


def test_omp_and_pi_capabilities_are_verified(env) -> None:
    _store, _ws, _home = env
    for agent in ("omp", "pi"):
        caps = ADAPTERS[agent].capabilities()
        assert caps.verified is True
        assert caps.receive_message and caps.idle_wake


def test_install_writes_agentctl_owned_assets_only(env) -> None:
    store, ws, home = env
    user_settings = Path(home.parent) / "userhome" / ".claude" / "settings.json"
    result = install(store, ws, "claude")
    assert Path(result["hook_path"]).exists()
    settings = Path(str(result["settings_path"]))
    assert settings.exists()
    assert home in settings.parents, "the fragment lives in agentctl's own home"
    assert not user_settings.exists(), "the user's settings file is never created or edited"


def test_install_is_idempotent(env) -> None:
    store, ws, _home = env
    first = install(store, ws, "omp")
    second = install(store, ws, "omp")
    assert first["hook_path"] == second["hook_path"]
    assert second["changed"] is False
    assert Path(second["hook_path"]).read_text() == Path(first["hook_path"]).read_text()


def test_launcher_carries_its_own_home_and_interpreter(env) -> None:
    store, ws, home = env
    result = install(store, ws, "omp")
    launcher = Path(result["launcher"])
    text = launcher.read_text()
    assert launcher.exists()
    assert str(home) in text, "the hook must read the registry that installed it"
    assert "python" in text
    assert launcher.stat().st_mode & 0o111, "launcher must be executable"


def test_hook_spools_and_registers_a_flag(env) -> None:
    store, ws, home = env
    result = install(store, ws, "omp")
    source = Path(result["hook_path"]).read_text()
    # Measured: pi.exec cannot spawn from an `input` handler, so the hook must
    # report through a file and never through a subprocess.
    assert "pi.exec" not in source
    assert 'appendFileSync' in source
    assert str(home / "spool") in source
    assert "registerFlag" in source, "an eager load needs a declared flag or shortcut"


def test_hook_never_throws_into_the_host(env) -> None:
    store, ws, _home = env
    source = Path(install(store, ws, "omp")["hook_path"]).read_text()
    assert "catch {" in source, "a failed spool write must stay invisible"
    assert "match(/\\[\\[agentctl:(msg_" in source, "the marker regex must be live JS"


def test_launch_args_point_at_the_asset(env) -> None:
    store, ws, _home = env
    result = install(store, ws, "pi")
    assert result["launch_args"] == ["-e", result["hook_path"]]
    claude = install(store, ws, "claude")
    assert claude["launch_args"][0] == "--settings"


def test_uninstall_removes_assets(env) -> None:
    store, ws, _home = env
    result = install(store, ws, "omp")
    removed = uninstall(store, ws, "omp")
    assert result["hook_path"] in removed["removed"]
    assert not Path(result["hook_path"]).exists()


def test_unknown_agent_is_refused(env) -> None:
    store, ws, _home = env
    with pytest.raises(AgentctlError):
        install(store, ws, "nonexistent")


def test_describe_all_lists_unsupported_agents_with_a_reason(env) -> None:
    store, ws, _home = env
    rows = {row["agent"]: row for row in describe_all(store, ws)}
    assert rows["codex"]["supported"] is False
    assert "no hook API" in rows["codex"]["capabilities"]["notes"]
    assert rows["omp"]["supported"] is True
