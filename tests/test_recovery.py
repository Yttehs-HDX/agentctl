"""Leader recovery: identity and sessions survive a restart of the CLI."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agentctl.cli import app
from agentctl.config import default_config, save_config
from agentctl.storage import Store, open_database
import agentctl.agents.detector as detector

runner = CliRunner()


def _configure(monkeypatch, default_agent: str = "omp") -> None:
    monkeypatch.setattr(detector, "installed", lambda d: d.id == "omp")
    monkeypatch.setattr(detector, "installed_command", lambda command: command[0] == "omp")
    config = default_config()
    config.defaults.agent = default_agent
    save_config(config)


def _run(*args: str):
    return runner.invoke(app, list(args))


def test_workspace_identity_survives_leader_restart(workspace: Path, fake_hc: Path, monkeypatch) -> None:
    _configure(monkeypatch)
    first = _run("--workspace", str(workspace), "--json", "init")
    assert first.exit_code == 0, first.output
    body = json.loads(first.output)["workspace"]

    # A "new leader process" is just another CLI invocation: same path, new store.
    second = _run("--workspace", str(workspace), "--json", "init")
    assert second.exit_code == 0
    again = json.loads(second.output)["workspace"]
    assert again["id"] == body["id"]
    assert again["leader_id"] == body["leader_id"]
    assert json.loads(second.output)["created"] is False


def test_sessions_and_terminals_survive_leader_restart(
    workspace: Path, fake_hc: Path, monkeypatch
) -> None:
    _configure(monkeypatch)
    _run("--workspace", str(workspace), "init")
    created = _run(
        "--workspace", str(workspace), "--json", "create", "--name", "persist", "--cwd", str(workspace)
    )
    assert created.exit_code == 0, created.output
    session = json.loads(created.output)["session"]

    listed = _run("--workspace", str(workspace), "--json", "list")
    assert listed.exit_code == 0, listed.output
    sessions = json.loads(listed.output)["sessions"]
    assert [s["name"] for s in sessions] == ["persist"]
    assert sessions[0]["id"] == session["id"]
    assert sessions[0]["hc_terminal_id"] == session["hc_terminal_id"]
    assert sessions[0]["status"] in {"idle", "starting"}


def test_recovery_runs_for_existing_sessions(workspace: Path, fake_hc: Path, monkeypatch) -> None:
    _configure(monkeypatch)
    _run("--workspace", str(workspace), "init")
    _run("--workspace", str(workspace), "create", "--name", "old", "--cwd", str(workspace))
    result = _run("--workspace", str(workspace), "--json", "recover")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["results"][0]["session"] == "old"
    assert payload["results"][0]["result"] == "attached"


def test_info_reports_counts_and_runtime(workspace: Path, fake_hc: Path, monkeypatch) -> None:
    _configure(monkeypatch)
    _run("--workspace", str(workspace), "init")
    _run("--workspace", str(workspace), "create", "--name", "one", "--cwd", str(workspace))
    result = _run("--workspace", str(workspace), "--json", "info")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["sessions_total"] == 1
    assert payload["runtime"]["available"] is True
    assert payload["runtime"]["version"] == "9.9.9-test"


def test_workspaces_do_not_share_sessions(workspace: Path, tmp_path: Path, fake_hc: Path, monkeypatch) -> None:
    _configure(monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    _run("--workspace", str(workspace), "init")
    _run("--workspace", str(other), "init")
    _run("--workspace", str(workspace), "create", "--name", "mine", "--cwd", str(workspace))

    listed = _run("--workspace", str(other), "--json", "list")
    assert json.loads(listed.output)["sessions"] == []

    status = _run("--workspace", str(other), "status", "mine")
    assert status.exit_code != 0


def test_rebind_after_directory_move_updates_existing_sessions(
    workspace: Path, tmp_path: Path, fake_hc: Path, monkeypatch
) -> None:
    _configure(monkeypatch)
    _run("--workspace", str(workspace), "init")
    created = _run(
        "--workspace",
        str(workspace),
        "--json",
        "create",
        "--name",
        "moved-worker",
        "--cwd",
        str(workspace / "backend"),
    )
    assert created.exit_code == 0, created.output
    session_id = json.loads(created.output)["session"]["id"]
    old_root = str(workspace)
    moved = tmp_path / "moved"
    workspace.rename(moved)

    result = _run("--workspace", old_root, "--json", "rebind", str(moved))

    assert result.exit_code == 0, (result.output, result.exception)
    with open_database() as conn:
        store = Store(conn)
        rebound = store.workspaces.find_by_root(str(moved))
        assert rebound is not None
        session = store.sessions.find_by_id(session_id)
        assert session.cwd == str(moved / "backend")
    listed = _run("--workspace", str(moved), "--json", "list", "--no-refresh")
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)["sessions"][0]["cwd"] == str(moved / "backend")
