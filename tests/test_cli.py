"""CLI contract: JSON envelope, argv handling, schema version."""

from __future__ import annotations

import json
import sys

import pytest

import agentctl.cli as cli
from agentctl.errors import AgentctlError
from agentctl.storage.models import Session


def test_prepare_argv_hoists_json_before_the_command() -> None:
    argv, json_mode = cli._prepare_argv(["send", "w1", "hello", "--json"])
    assert json_mode is True
    assert argv == ["--json", "send", "w1", "hello"]


def test_prepare_argv_stops_at_the_terminator() -> None:
    argv, json_mode = cli._prepare_argv(["send", "w1", "--", "--json"])
    assert json_mode is False
    assert argv == ["send", "w1", "--", "--json"]


def test_error_envelope_carries_the_schema() -> None:
    body = AgentctlError("boom").to_dict()
    assert body["schema"] == 1
    assert body["error"]["code"] == "error"


def test_session_dict_exposes_worktree_identity() -> None:
    session = Session(
        id="sess_x",
        workspace_id="ws_x",
        name="worker",
        agent="omp",
        cwd="/repo/wt",
        mode="worktree",
        hc_terminal_id="t1",
        hc_name="ac-worker-1",
        native_session_id="01a2",
        native_session_dir="/state/sess_x",
        worktree_path="/repo/.agentctl/worktrees/sess_x",
        worktree_branch="agentctl/worker-x",
        git_root="/repo",
        runtime_started_at="2026-01-01T00:00:00+00:00",
        status="idle",
        last_error=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        deleted_at=None,
    )
    body = session.to_dict()
    assert body["hc_name"] == "ac-worker-1"
    assert body["git_root"] == "/repo"


def test_json_usage_error_is_machine_readable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["agentctl", "--json", "send"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    body = json.loads(capsys.readouterr().err)
    assert body["schema"] == 1
    assert body["error"]["code"] == "usage"
    assert "session" in body["error"]["message"]


def test_json_help_still_exits_zero(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["agentctl", "--json", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0


def test_json_after_terminator_stays_human(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["agentctl", "send", "x", "--", "--json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 4
    stderr = capsys.readouterr().err
    assert stderr.startswith("error: ")
    assert not stderr.startswith("{")


def test_json_before_the_command_still_emits_the_envelope(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["agentctl", "--json", "status", "x"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 4
    body = json.loads(capsys.readouterr().err)
    assert body["schema"] == 1
    assert body["error"]["code"] == "setup_required"


def _setup_payload(version_verified) -> dict:
    return {
        "config": "/tmp/config.toml",
        "runtime": {"installed": True, "version": "0.1.22"},
        "agents": [
            {
                "id": "claude",
                "command": ["claude"],
                "installed": True,
                "version": "1.0.0",
                "version_verified": version_verified,
                "enabled": True,
                "integration": True,
            }
        ],
        "default_agent": None,
    }


@pytest.mark.parametrize(
    ("version_verified", "expected", "absent"),
    [
        (True, "installed", ("(unverified)", "(unmeasured)")),
        (False, "installed (unverified)", ("(unmeasured)",)),
        (None, "installed (unmeasured)", ("(unverified)",)),
    ],
)
def test_print_setup_distinguishes_verification(
    version_verified, expected, absent, capsys
) -> None:
    cli._print_setup(_setup_payload(version_verified))
    stdout = capsys.readouterr().out
    assert expected in stdout
    for marker in absent:
        assert marker not in stdout


def test_print_setup_leaves_missing_agents_unqualified(capsys) -> None:
    payload = _setup_payload(None)
    payload["agents"][0]["installed"] = False
    payload["agents"][0]["version"] = None
    cli._print_setup(payload)
    stdout = capsys.readouterr().out
    assert "missing" in stdout
    assert "(unmeasured)" not in stdout
    assert "(unverified)" not in stdout


def test_agents_inspect_unknown_id_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["agentctl", "agents", "inspect", "nope"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 6
    assert "nope" in capsys.readouterr().err


def test_json_usage_error_survives_a_narrow_terminal(monkeypatch, capsys) -> None:
    """Click wraps its error panel at the terminal width.

    At `COLUMNS=30` the reason `Missing argument 'session'.` reaches the parser
    as two lines. Taking only the first line dropped the argument name, so the
    machine-readable message said `Missing argument` and nothing else.
    """
    monkeypatch.setenv("COLUMNS", "30")
    monkeypatch.setattr(sys, "argv", ["agentctl", "--json", "send"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    body = json.loads(capsys.readouterr().err)
    assert body["error"]["message"] == "Missing argument 'session'."


def test_json_usage_error_survives_forced_color(monkeypatch, capsys) -> None:
    """typer forces color on under `GITHUB_ACTIONS`, wrapping every `│`.

    The panel's content line then starts with `\\x1b[31m│`, so a `startswith`
    check on the border character misses it and the parser falls back to the
    usage line.
    """
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(sys, "argv", ["agentctl", "--json", "send"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    body = json.loads(capsys.readouterr().err)
    assert body["error"]["message"] == "Missing argument 'session'."
