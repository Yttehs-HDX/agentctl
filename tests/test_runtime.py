"""Runtime adapter: command construction, JSON parsing, error mapping.

Uses the fake `hc` fixture from conftest so no real core is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentctl.errors import (
    RuntimeBlocked,
    RuntimeFailure,
    RuntimeFinished,
    RuntimeNotFound,
    RuntimeUnavailable,
)
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from tests.conftest import calls, read_state


def test_version_reads_hc_output(fake_hc: Path) -> None:
    assert HerdrCoreAdapter().version() == "9.9.9-test"


def test_create_terminal_uses_bg_cwd_name_and_labels(fake_hc: Path, workspace: Path) -> None:
    adapter = HerdrCoreAdapter()
    terminal = adapter.create_terminal(
        ["omp"],
        cwd=str(workspace / "backend"),
        name="ac-worker-123456",
        labels={"agentctl.workspace": "ws_1", "agentctl.session": "sess_1"},
    )
    assert terminal.address == "t1"
    assert terminal.state == "live"
    argv = calls(fake_hc)[-1]
    assert argv[:1] == ["run"]
    assert "--bg" in argv and "--json" in argv and "--no-parent" in argv
    assert argv[argv.index("--cwd") + 1] == str(workspace / "backend")
    assert argv[argv.index("--name") + 1] == "ac-worker-123456"
    assert "agentctl.workspace=ws_1" in argv
    # The program itself comes last, after `--`.
    assert argv[argv.index("--") + 1 :] == ["omp"]


def test_hc_terminal_id_is_environment_independent(fake_hc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HC_TERMINAL_ID", "t_parent")
    adapter = HerdrCoreAdapter()
    adapter.create_terminal(["omp"], cwd="/", name="ac-x-1", labels={})
    assert "HC_TERMINAL_ID" not in adapter._env()


def test_send_passes_text_verbatim(fake_hc: Path, workspace: Path) -> None:
    adapter = HerdrCoreAdapter()
    terminal = adapter.create_terminal(["omp"], cwd=str(workspace), name="ac-a-1", labels={})
    text = '-leading "quotes" $VAR\nline two'
    result = adapter.send_input(terminal.address, text)
    assert result["started"] is True
    sent = read_state(fake_hc)["sent"][-1]
    assert sent["text"] == text
    argv = calls(fake_hc)[-1]
    assert argv[-1] == text
    assert argv[argv.index("--") + 1] == terminal.address


def test_read_output_shape(fake_hc: Path) -> None:
    output = HerdrCoreAdapter().read_output("t1", lines=20)
    assert output.text == "screen for t1"
    assert output.complete is True


def test_missing_terminal_maps_to_runtime_not_found(fake_hc: Path) -> None:
    with pytest.raises(RuntimeNotFound):
        HerdrCoreAdapter().get_terminal("t_missing")


def test_error_envelope_maps_to_specific_errors(fake_hc: Path) -> None:
    import subprocess

    adapter = HerdrCoreAdapter()

    def proc(kind: str, code: int) -> subprocess.CompletedProcess[str]:
        body = json.dumps({"error": {"kind": kind, "message": "msg", "hint": "h"}})
        return subprocess.CompletedProcess(args=["hc"], returncode=code, stdout="", stderr=body)

    assert isinstance(adapter._map_error(proc("not_found", 3)), RuntimeNotFound)
    assert isinstance(adapter._map_error(proc("blocked", 9)), RuntimeBlocked)
    assert isinstance(adapter._map_error(proc("finished", 11)), RuntimeFinished)
    plain = adapter._map_error(proc("weird", 1))
    assert isinstance(plain, RuntimeFailure)
    assert plain.kind == "weird"


def test_unparseable_output_is_a_runtime_failure(fake_hc: Path) -> None:
    from agentctl.runtime.herdr_core import _parse_json

    with pytest.raises(RuntimeFailure):
        _parse_json("this is not json")


def test_not_installed_runtime_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    adapter = HerdrCoreAdapter("hc")
    assert adapter.available() is False
    with pytest.raises(RuntimeUnavailable):
        adapter.list_terminals()


def test_list_terminals_filters_by_label(fake_hc: Path, workspace: Path) -> None:
    adapter = HerdrCoreAdapter()
    adapter.create_terminal(["omp"], cwd=str(workspace), name="a", labels={"agentctl.session": "s1"})
    adapter.create_terminal(["omp"], cwd=str(workspace), name="b", labels={"agentctl.session": "s2"})
    found = adapter.list_terminals(labels={"agentctl.session": "s2"})
    assert [t.name for t in found] == ["b"]


def test_kill_then_remove(fake_hc: Path, workspace: Path) -> None:
    adapter = HerdrCoreAdapter()
    terminal = adapter.create_terminal(["omp"], cwd=str(workspace), name="k", labels={})
    killed = adapter.kill(terminal.address)
    assert killed.live is False
    adapter.remove(terminal.address)
    assert adapter.find_terminal(terminal.address) is None
