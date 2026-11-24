"""CLI contract: JSON envelope, argv handling, schema version."""

from __future__ import annotations

import json
import os
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
    monkeypatch.delenv("AGENTCTL_JSON", raising=False)
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main()
    finally:
        os.environ.pop("AGENTCTL_JSON", None)
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
