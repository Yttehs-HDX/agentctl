"""Leader binding: who leads, liveness from Herdr, and no Worker takeover."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agentctl.agents.detector as detector
from agentctl.config import default_config
from agentctl.errors import LeaderRequired, UsageError
from agentctl.leader.service import LEADER_ROLE, ROLE_LABEL, WORKER_ROLE, LeaderService
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.service import CreateRequest, SessionService
from agentctl.storage import Store, open_connection
from agentctl.workspace.resolver import WorkspaceResolver
from tests.conftest import read_state


@pytest.fixture()
def env(tmp_path: Path, workspace: Path, fake_hc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(detector, "installed", lambda definition: True)
    monkeypatch.setattr(detector, "installed_command", lambda command: True)
    monkeypatch.delenv("HC_TERMINAL_ID", raising=False)
    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(workspace))
    runtime = HerdrCoreAdapter()
    config = default_config()
    config.defaults.agent = "omp"
    sessions = SessionService(store, runtime, config)
    yield store, ws, runtime, sessions, fake_hc
    conn.close()


def _add_terminal(state_file: Path, address: str, labels: dict[str, str], *, state: str = "live") -> None:
    # The fake hc writes this file on its first call; a test that seeds a
    # terminal before any call starts from the same empty shape.
    current = json.loads(state_file.read_text()) if state_file.exists() else {"calls": [], "terminals": {}}
    current["terminals"][address] = {
        "address": address,
        "name": f"term-{address}",
        "state": state,
        "status": {"app": "omp", "state": "idle"},
        "reported": True,
        "cwd": "/tmp",
        "labels": dict(labels),
        "turn": 0,
    }
    state_file.write_text(json.dumps(current))


def test_attach_without_a_terminal_is_an_outside_leader(env) -> None:
    store, ws, runtime, _sessions, _hc = env
    binding = LeaderService(store, runtime).attach(ws)
    assert binding.terminal is None
    assert binding.workspace_id == ws.id
    assert binding.leader_id == ws.leader_id


def test_attach_records_the_leader_terminal_and_labels_it(env) -> None:
    store, ws, runtime, _sessions, state_file = env
    _add_terminal(state_file, "t1", {})
    binding = LeaderService(store, runtime).attach(ws, terminal="t1")
    assert binding.terminal == "t1"
    state = json.loads(state_file.read_text())
    assert state["terminals"]["t1"]["labels"][ROLE_LABEL] == LEADER_ROLE
    assert state["terminals"]["t1"]["labels"]["agentctl.workspace"] == ws.id


def test_name_based_legacy_binding_can_be_reattached(env) -> None:
    from agentctl.workspace.service import WorkspaceService

    store, ws, runtime, _sessions, state_file = env
    _add_terminal(state_file, "tz", {})
    service = LeaderService(store, runtime)
    service.attach(ws, terminal="tz")
    store.conn.execute(
        "UPDATE leader_bindings SET terminal = ? WHERE workspace_id = ?", ("term-tz", ws.id)
    )

    assert WorkspaceService(store, runtime).info(ws).orphan_terminals == []
    assert service.attach(ws, terminal="term-tz").terminal == "tz"


def test_a_worker_terminal_cannot_become_the_leader(env) -> None:
    store, ws, runtime, sessions, state_file = env
    created = sessions.create(ws, CreateRequest(name="w1", agent="omp", cwd=ws.root_path))
    address = created.terminal.address
    with pytest.raises(LeaderRequired) as excinfo:
        LeaderService(store, runtime).attach(ws, terminal=address)
    assert "is a Worker" in excinfo.value.message


def test_a_legacy_worker_without_a_role_cannot_become_leader(env) -> None:
    store, ws, runtime, _sessions, state_file = env
    _add_terminal(state_file, "tz", {"agentctl.workspace": ws.id, "agentctl.session": "sess_legacy"})
    with pytest.raises(LeaderRequired):
        LeaderService(store, runtime).attach(ws, terminal="tz")


def test_worker_terminals_carry_the_worker_role_and_outlive_the_parent(env) -> None:
    store, ws, runtime, sessions, state_file = env
    created = sessions.create(ws, CreateRequest(name="w1", agent="omp", cwd=ws.root_path))
    state = json.loads(state_file.read_text())
    # The fake hc keys terminals by the launched --name, which agentctl derives
    # from the session id (`ac-<slug>-<id6>`), not from the user-facing name.
    body = next(b for b in state["terminals"].values() if b["address"] == created.terminal.address)
    assert body["address"] == created.terminal.address
    assert body["labels"][ROLE_LABEL] == WORKER_ROLE
    assert body["labels"]["agentctl.session"] == created.session.id
    assert body["die_with_parent"] is False, "a Worker must outlive its Leader"
    assert body["parent"] is None, "no Leader is bound, so there is no parent"


def test_second_leader_is_refused_while_the_first_is_live(env) -> None:
    store, ws, runtime, _sessions, state_file = env
    _add_terminal(state_file, "t1", {})
    _add_terminal(state_file, "t2", {})
    service = LeaderService(store, runtime)
    service.attach(ws, terminal="t1")
    with pytest.raises(LeaderRequired):
        service.attach(ws, terminal="t2")


def test_a_dead_terminal_frees_the_binding(env) -> None:
    store, ws, runtime, _sessions, state_file = env
    _add_terminal(state_file, "t1", {})
    _add_terminal(state_file, "t2", {})
    service = LeaderService(store, runtime)
    service.attach(ws, terminal="t1")
    status = service.status(ws)
    assert status.live is True

    # Herdr says the terminal finished: the binding is stale, nothing to expire.
    state = json.loads(state_file.read_text())
    state["terminals"]["t1"]["state"] = "finished"
    state_file.write_text(json.dumps(state))
    assert service.status(ws).live is False
    replaced = service.attach(ws, terminal="t2")
    assert replaced.terminal == "t2"


def test_takeover_replaces_a_live_binding_explicitly(env) -> None:
    store, ws, runtime, _sessions, state_file = env
    _add_terminal(state_file, "t1", {})
    _add_terminal(state_file, "t2", {})
    service = LeaderService(store, runtime)
    service.attach(ws, terminal="t1")
    binding = service.attach(ws, terminal="t2", take_over=True)
    assert binding.terminal == "t2"


def test_status_explains_an_unbound_workspace(env) -> None:
    store, ws, runtime, _sessions, _hc = env
    status = LeaderService(store, runtime).status(ws)
    assert status.binding is None
    assert status.live is False
    assert "no Leader has attached" in (status.reason or "")
    assert status.to_dict()["bound"] is False


def test_attaching_to_an_unknown_terminal_is_rejected(env) -> None:
    store, ws, runtime, _sessions, _hc = env
    with pytest.raises(UsageError):
        LeaderService(store, runtime).attach(ws, terminal="t99")


def test_terminal_from_another_workspace_is_rejected(env) -> None:
    store, ws, runtime, _sessions, state_file = env
    _add_terminal(state_file, "t1", {"agentctl.workspace": "ws_other"})
    with pytest.raises(UsageError) as excinfo:
        LeaderService(store, runtime).attach(ws, terminal="t1")
    assert "belongs to workspace" in excinfo.value.message


def test_detach_forgets_the_binding(env) -> None:
    store, ws, runtime, _sessions, _hc = env
    service = LeaderService(store, runtime)
    service.attach(ws)
    assert service.detach(ws) is True
    assert service.current(ws) is None
    assert service.detach(ws) is False


def test_workspaces_are_isolated(env, tmp_path: Path) -> None:
    store, ws_a, runtime, _sessions, _hc = env
    other_root = tmp_path / "other"
    other_root.mkdir()
    ws_b, _ = WorkspaceResolver(store).get_or_create(str(other_root.resolve()))
    service = LeaderService(store, runtime)
    service.attach(ws_a)
    assert service.current(ws_b) is None
    service.attach(ws_b)
    assert service.current(ws_a) is not None
    assert service.current(ws_b) is not None


def test_reused_address_does_not_count_as_the_bound_leader(env) -> None:
    store, ws, runtime, sessions, state_file = env
    _add_terminal(state_file, "tz", {})
    leader = LeaderService(store, runtime)
    leader.attach(ws, terminal="tz")
    _add_terminal(state_file, "tz", {})

    assert not leader.status(ws).live
    worker = sessions.create(ws, CreateRequest(name="w", agent="omp", cwd=ws.root_path))
    terminals = read_state(state_file)["terminals"]
    assert terminals[worker.terminal.name]["parent"] is None


def test_info_distinguishes_the_bound_leader_from_a_previous_leader(env) -> None:
    from agentctl.workspace.service import WorkspaceService

    store, ws, runtime, _sessions, state_file = env
    _add_terminal(state_file, "ty", {})
    _add_terminal(state_file, "tz", {})
    leader = LeaderService(store, runtime)
    leader.attach(ws, terminal="ty")
    assert WorkspaceService(store, runtime).info(ws).orphan_terminals == []
    leader.attach(ws, terminal="tz", take_over=True)
    assert WorkspaceService(store, runtime).info(ws).orphan_terminals == ["ty"]


def test_wait_after_takeover_reports_only_the_new_leaders_children(env) -> None:
    from typer.testing import CliRunner

    from agentctl.cli import app
    from agentctl.config import save_config

    store, ws, runtime, sessions, state_file = env
    _add_terminal(state_file, "ty", {})
    _add_terminal(state_file, "tz", {})
    leader = LeaderService(store, runtime)
    leader.attach(ws, terminal="ty")
    sessions.create(ws, CreateRequest(name="old-worker", agent="omp", cwd=ws.root_path))
    leader.attach(ws, terminal="tz", take_over=True)
    save_config(default_config())

    runner = CliRunner()
    arguments = ["--workspace", ws.root_path, "--json", "wait", "--all", "--timeout", "1"]
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["scope"] == "leader_children"
    assert body["parent"] == "tz"
    assert body["children"] == []

    current = sessions.create(ws, CreateRequest(name="new-worker", agent="omp", cwd=ws.root_path))
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert [child["terminal"] for child in json.loads(result.stdout)["children"]] == [current.terminal.address]

    leader.detach(ws)
    result = runner.invoke(app, arguments)
    assert isinstance(result.exception, LeaderRequired)


def test_attach_after_a_concurrent_commit_is_refused(env) -> None:
    store, ws, runtime, _sessions, _state = env
    service = LeaderService(store, runtime)
    first = runtime.create_terminal(["sleep", "60"], cwd=ws.root_path, name="lead-a")
    service.attach(ws, terminal=first.address)
    second = runtime.create_terminal(["sleep", "60"], cwd=ws.root_path, name="lead-b")

    # The first attach's row and label are committed together, so a second
    # attacher that validated against a pre-commit read must still refuse.
    with pytest.raises(LeaderRequired, match="already led"):
        service.attach(ws, terminal=second.address)

    binding = store.leaders.get(ws.id)
    assert binding is not None
    assert binding.terminal == first.address
    loser = runtime.find_terminal(second.address)
    assert loser is not None
    assert loser.labels.get(ROLE_LABEL) != LEADER_ROLE


def test_attach_labels_inside_the_write_transaction(env, monkeypatch) -> None:
    store, ws, runtime, _sessions, _state = env
    seen: dict[str, object] = {}
    original = runtime.label

    def label(address, labels):
        seen["in_transaction"] = store.conn.in_transaction
        seen["row_visible"] = store.leaders.get(ws.id) is not None
        return original(address, labels)

    monkeypatch.setattr(runtime, "label", label)
    terminal = runtime.create_terminal(["sleep", "60"], cwd=ws.root_path, name="lead")
    LeaderService(store, runtime).attach(ws, terminal=terminal.address)
    assert seen["in_transaction"] is True
    assert seen["row_visible"] is True
