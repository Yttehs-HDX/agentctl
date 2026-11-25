"""Session service: creation, validation, rollback, deletion."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentctl.config import default_config
from agentctl.errors import (
    AgentNotConfigured,
    UsageError,
    AgentUnknown,
    InvalidPath,
    SessionNameConflict,
    SessionNotFound,
)
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.service import CreateRequest, SessionService
from agentctl.storage import Store, open_connection
from agentctl.workspace.resolver import WorkspaceResolver
import agentctl.agents.detector as detector
from tests.conftest import calls


def _last_run_argv() -> list[str]:
    """The most recent `hc run` invocation, ignoring status refreshes."""
    return [call for call in calls(_fake_hc_state()) if call and call[0] == "run"][-1]


def _fake_hc_state() -> Path:
    """The fake hc's state file, derived from PATH (it lives beside the script)."""
    import os

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(entry) / "hc"
        if candidate.exists() and "fake-hc-state.json" in candidate.read_text(errors="ignore"):
            return candidate.parent.parent / "fake-hc-state.json"
    raise AssertionError("the fake hc is not on PATH")


@pytest.fixture()
def setup(workspace: Path, fake_hc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(detector, "installed", lambda d: d.id == "omp")
    monkeypatch.setattr(detector, "installed_command", lambda command: command[0] == "omp")
    conn = open_connection()
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(workspace))
    config = default_config()
    config.defaults.agent = "omp"
    service = SessionService(store, HerdrCoreAdapter(), config)
    yield service, store, ws, config
    conn.close()


def test_create_records_identity_and_terminal(setup, workspace: Path) -> None:
    service, store, ws, _config = setup
    result = service.create(ws, CreateRequest(name="boot", agent=None, cwd=str(workspace / "backend")))
    session = result.session
    assert session.id.startswith("sess_")
    assert session.workspace_id == ws.id
    assert session.agent == "omp"
    assert session.cwd == str(workspace / "backend")
    assert session.mode == "shared"
    assert result.terminal.address
    assert session.hc_terminal_id == result.terminal.address
    assert result.ready is True
    stored = store.sessions.find_by_id(session.id)
    assert stored is not None
    assert stored.hc_terminal_id == session.hc_terminal_id


def test_session_id_never_equals_terminal_id(setup, workspace: Path) -> None:
    service, _store, ws, _config = setup
    result = service.create(ws, CreateRequest(name="ids", agent="omp", cwd=str(workspace)))
    assert result.session.id != result.terminal.address
    assert not result.session.id.startswith("t")


def test_duplicate_name_is_rejected(setup, workspace: Path) -> None:
    service, _store, ws, _config = setup
    service.create(ws, CreateRequest(name="dup", agent=None, cwd=str(workspace)))
    with pytest.raises(SessionNameConflict):
        service.create(ws, CreateRequest(name="dup", agent=None, cwd=str(workspace)))


def test_same_name_allowed_in_another_workspace(setup, tmp_path: Path, workspace: Path) -> None:
    service, store, ws, _config = setup
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = WorkspaceResolver(store).get_or_create(str(other_root))[0]
    service.create(ws, CreateRequest(name="same", agent=None, cwd=str(workspace)))
    service.create(other, CreateRequest(name="same", agent=None, cwd=str(other_root)))
    assert len(store.sessions.list_for_workspace(ws.id)) == 1


def test_cwd_outside_workspace_is_rejected(setup, tmp_path: Path) -> None:
    service, _store, ws, _config = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(InvalidPath):
        service.create(ws, CreateRequest(name="escape", agent=None, cwd=str(outside)))


def test_cwd_symlink_escape_is_rejected(setup, workspace: Path, tmp_path: Path) -> None:
    service, _store, ws, _config = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = workspace / "escape"
    escape.symlink_to(outside)
    with pytest.raises(InvalidPath):
        service.create(ws, CreateRequest(name="link", agent=None, cwd=str(escape)))


def test_missing_cwd_is_rejected(setup, workspace: Path) -> None:
    service, _store, ws, _config = setup
    with pytest.raises(InvalidPath):
        service.create(ws, CreateRequest(name="gone", agent=None, cwd=str(workspace / "nope")))


def test_no_agent_and_no_default_errors(setup, workspace: Path) -> None:
    service, _store, ws, config = setup
    config.defaults.agent = ""
    with pytest.raises(AgentNotConfigured):
        service.create(ws, CreateRequest(name="noagent", agent=None, cwd=str(workspace)))


def test_workspace_default_beats_global(setup, workspace: Path) -> None:
    service, store, ws, config = setup
    config.defaults.agent = "omp"
    ws.default_agent = "omp"
    assert service.resolve_agent(ws, None) == "omp"
    ws.default_agent = None


def test_explicit_agent_does_not_change_default(setup, workspace: Path) -> None:
    service, _store, ws, config = setup
    service.create(ws, CreateRequest(name="override", agent="omp", cwd=str(workspace)))
    assert config.defaults.agent == "omp"


def test_unknown_agent_rejected(setup, workspace: Path) -> None:
    service, _store, ws, _config = setup
    with pytest.raises(AgentUnknown):
        service.create(ws, CreateRequest(name="x", agent="ghost", cwd=str(workspace)))


def test_create_rolls_back_when_runtime_fails(
    setup, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentctl.errors import RuntimeFailure

    service, store, ws, _config = setup

    def boom(*args, **kwargs):
        raise RuntimeFailure("spawn failed", kind="spawn_failed")

    monkeypatch.setattr(HerdrCoreAdapter, "create_terminal", boom)
    with pytest.raises(RuntimeFailure):
        service.create(ws, CreateRequest(name="broken", agent=None, cwd=str(workspace)))
    assert store.sessions.find_in_workspace(ws.id, "broken") is None
    assert store.sessions.list_for_workspace(ws.id) == []


def test_delete_kills_and_retires(setup, workspace: Path) -> None:
    service, store, ws, _config = setup
    result = service.create(ws, CreateRequest(name="victim", agent=None, cwd=str(workspace)))
    deleted = service.delete(ws, "victim")
    assert deleted.hc_terminal_id is None
    assert store.sessions.find_in_workspace(ws.id, "victim") is None
    history = store.sessions.list_for_workspace(ws.id, include_deleted=True)
    assert history[0].id == result.session.id
    assert history[0].deleted_at is not None


def test_delete_unknown_session_raises(setup) -> None:
    service, _store, ws, _config = setup
    with pytest.raises(SessionNotFound):
        service.delete(ws, "nothing")


def test_get_is_scoped_to_workspace(setup, workspace: Path, tmp_path: Path) -> None:
    service, store, ws, _config = setup
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = WorkspaceResolver(store).get_or_create(str(other_root))[0]
    service.create(other, CreateRequest(name="theirs", agent=None, cwd=str(other_root)))
    with pytest.raises(SessionNotFound):
        service.get(ws, "theirs")


def test_names_are_reusable_after_delete(setup, workspace: Path) -> None:
    service, _store, ws, _config = setup
    service.create(ws, CreateRequest(name="cycle", agent=None, cwd=str(workspace)))
    service.delete(ws, "cycle")
    service.create(ws, CreateRequest(name="cycle", agent=None, cwd=str(workspace)))
    assert service.get(ws, "cycle").name == "cycle"


def test_hc_name_is_valid_for_the_runtime(setup, workspace: Path) -> None:
    import re

    service, _store, ws, _config = setup
    result = service.create(ws, CreateRequest(name="My Weird/Name", agent=None, cwd=str(workspace)))
    name = result.session.hc_name
    assert name is not None
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", name)
    assert len(name) <= 32


def test_create_ready_is_false_without_a_reported_status(
    setup, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentctl.runtime.base import TerminalInfo

    service, _store, ws, _config = setup
    unreported = TerminalInfo(
        address="t99",
        name=None,
        state="live",
        status=None,
        app=None,
        cwd=str(workspace),
        exit_code=None,
        reason=None,
        reported=False,
    )
    monkeypatch.setattr(service.lifecycle, "find_terminal", lambda session: unreported)
    result = service.create(ws, CreateRequest(name="quiet", agent="omp", cwd=str(workspace)))
    assert result.ready is False


def test_create_wait_uses_the_requested_timeout(
    setup, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentctl.runtime.base import TerminalInfo

    service, _store, ws, _config = setup
    reported = TerminalInfo(
        address="t99",
        name=None,
        state="live",
        status="idle",
        app=None,
        cwd=str(workspace),
        exit_code=None,
        reason=None,
        reported=True,
    )
    seen: list[float] = []
    monkeypatch.setattr(
        service.lifecycle, "wait_until_ready", lambda session, timeout: seen.append(timeout)
    )
    monkeypatch.setattr(service.lifecycle, "find_terminal", lambda session: reported)
    result = service.create(
        ws, CreateRequest(name="waited", agent="omp", cwd=str(workspace)), wait_timeout=2.5
    )
    assert seen == [2.5]
    assert result.ready is True


def test_address_shaped_names_are_rejected(setup, workspace: Path) -> None:
    """Herdr shares one namespace between names and addresses.

    Measured on 0.1.18: `--name t7` is refused, so a session called `t7` must
    fail at the agentctl boundary with a hint instead of a raw runtime error.
    """
    from agentctl.session.service import is_hc_address_shaped

    service, store, ws, _config = setup
    for bad in ("t7", "t1", "t12b"):
        assert is_hc_address_shaped(bad) is True
    for good in ("worker", "t", "task7", "reviewer"):
        assert is_hc_address_shaped(good) is False

    with pytest.raises(UsageError) as excinfo:
        service.create(ws, CreateRequest(name="t7", agent="omp", cwd=str(workspace)))
    assert "shaped like a Herdr terminal address" in excinfo.value.message


def test_first_task_is_passed_at_launch(setup, workspace: Path) -> None:
    """A task at launch is not a send: a booting agent cannot drop it."""
    service, store, ws, _config = setup
    service.create(
        ws, CreateRequest(name="worker", agent="omp", cwd=str(workspace), task="map the layout")
    )
    argv = _last_run_argv()
    after_dash = argv[argv.index("--") + 1 :]
    assert after_dash[-1] == "map the layout"


def test_worker_is_parented_to_the_leader_and_outlives_it(setup, workspace: Path) -> None:
    """`--parent` gives Herdr's notes, `--outlive-parent` keeps it alive."""
    from agentctl.leader.service import LeaderService

    service, store, ws, _config = setup
    service.create(ws, CreateRequest(name="lone", agent="omp", cwd=str(workspace)))
    alone = _last_run_argv()
    assert "--no-parent" in alone, "no Leader bound, so no parent"
    assert "--outlive-parent" in alone

    leader = service.runtime.create_terminal(["sleep", "60"], cwd=str(workspace), name="leader")
    LeaderService(store, service.runtime).attach(ws, terminal=leader.address)
    service.create(ws, CreateRequest(name="child", agent="omp", cwd=str(workspace)))
    with_leader = _last_run_argv()
    assert with_leader[with_leader.index("--parent") + 1] == leader.address
    assert "--outlive-parent" in with_leader


def test_name_race_is_a_conflict_not_a_raw_integrity_error(
    setup, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentctl.paths import session_state_dir

    service, store, ws, _config = setup
    service.create(ws, CreateRequest(name="dup", agent="omp", cwd=str(workspace)))
    before = {p.name for p in Path(session_state_dir()).glob("sess_*")}
    # Simulate two processes passing the pre-check together: only the unique
    # index sees the collision.
    monkeypatch.setattr(store.sessions, "find_in_workspace", lambda *args, **kwargs: None)
    with pytest.raises(SessionNameConflict):
        service.create(ws, CreateRequest(name="dup", agent="omp", cwd=str(workspace)))
    assert len(store.sessions.list_for_workspace(ws.id)) == 1
    assert {p.name for p in Path(session_state_dir()).glob("sess_*")} == before


def test_failed_spawn_leaves_no_row_and_no_native_dir(
    setup, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentctl.errors import RuntimeSpawnFailed
    from agentctl.paths import session_state_dir

    service, store, ws, _config = setup

    def fail(*args, **kwargs):
        raise RuntimeSpawnFailed("spawn failed")

    monkeypatch.setattr(service.runtime, "create_terminal", fail)
    with pytest.raises(RuntimeSpawnFailed):
        service.create(ws, CreateRequest(name="dead", agent="omp", cwd=str(workspace)))
    assert store.sessions.find_in_workspace(ws.id, "dead") is None
    assert list(Path(session_state_dir()).glob("sess_*")) == []
