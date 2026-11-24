"""Leader lease: one holder at a time, expiry, takeover, fencing, isolation."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from agentctl.clock import now_precise, plus
from agentctl.errors import LeaderConflict, LeaderRequired, UsageError
from agentctl.leader.service import LeaderService, validate_instance_id
from agentctl.storage import Store, open_connection, open_database
from agentctl.workspace.resolver import WorkspaceResolver


def _workspace(tmp_path: Path, name: str = "proj"):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    conn = open_connection(tmp_path / "registry.db")
    store = Store(conn)
    ws, _ = WorkspaceResolver(store).get_or_create(str(root.resolve()))
    return conn, store, ws


def test_attach_creates_epoch_one(tmp_path: Path) -> None:
    conn, store, ws = _workspace(tmp_path)
    lease, created = LeaderService(store).attach(ws, instance_id="inst_a")
    assert created is True
    assert lease.leader_epoch == 1
    assert lease.leader_id == ws.leader_id
    assert lease.lease_token
    conn.close()


def test_second_instance_is_refused_while_lease_is_live(tmp_path: Path) -> None:
    conn, store, ws = _workspace(tmp_path)
    service = LeaderService(store)
    service.attach(ws, instance_id="inst_a")
    with pytest.raises(LeaderConflict) as excinfo:
        service.attach(ws, instance_id="inst_b")
    assert "already has a live Leader" in excinfo.value.message
    conn.close()


def test_same_instance_reattaches_without_bumping_epoch(tmp_path: Path) -> None:
    conn, store, ws = _workspace(tmp_path)
    service = LeaderService(store)
    first, _ = service.attach(ws, instance_id="inst_a")
    again, created = service.attach(ws, instance_id="inst_a")
    assert created is False
    assert again.leader_epoch == first.leader_epoch
    assert again.lease_token == first.lease_token
    conn.close()


def test_takeover_bumps_epoch_and_fences_the_old_token(tmp_path: Path) -> None:
    conn, store, ws = _workspace(tmp_path)
    service = LeaderService(store)
    old, _ = service.attach(ws, instance_id="inst_a")
    new = service.takeover(ws, instance_id="inst_b")
    assert new.leader_epoch == old.leader_epoch + 1
    assert new.lease_token != old.lease_token

    # The superseded instance cannot renew, release or act at all.
    for call in (
        lambda: service.renew(ws, old.lease_token, instance_id="inst_a"),
        lambda: service.require(ws, old.lease_token, instance_id="inst_a"),
    ):
        with pytest.raises(LeaderConflict):
            call()
    assert service.renew(ws, new.lease_token, instance_id="inst_b").leader_epoch == new.leader_epoch
    conn.close()


def test_lease_expiry_makes_it_claimable(tmp_path: Path) -> None:
    conn, store, ws = _workspace(tmp_path)
    service = LeaderService(store, lease_seconds=0.01)
    lease, _ = service.attach(ws, instance_id="inst_a")
    # Age the row rather than sleeping: expiry is a property of the stored time.
    store.leases.upsert(
        type(lease)(
            **{
                **{f: getattr(lease, f) for f in lease.__slots__},
                "expires_at": plus(now_precise(), -5),
            }
        )
    )
    status = service.status(ws)
    assert status.held is False
    assert status.expired is True
    assert service.current(ws).leader_instance_id == "inst_a"

    replacement, created = service.attach(ws, instance_id="inst_b")
    assert created is True
    assert replacement.leader_epoch == lease.leader_epoch + 1
    conn.close()


def test_renewal_extends_the_deadline(tmp_path: Path) -> None:
    conn, store, ws = _workspace(tmp_path)
    service = LeaderService(store, lease_seconds=60)
    lease, _ = service.attach(ws, instance_id="inst_a")
    renewed = service.renew(ws, lease.lease_token, instance_id="inst_a")
    assert renewed.expires_at >= lease.expires_at
    conn.close()


def test_release_frees_the_slot_and_invalidates_the_token(tmp_path: Path) -> None:
    conn, store, ws = _workspace(tmp_path)
    service = LeaderService(store)
    lease, _ = service.attach(ws, instance_id="inst_a")
    assert service.release(ws, lease.lease_token, instance_id="inst_a") is True
    with pytest.raises(LeaderConflict):
        service.require(ws, lease.lease_token, instance_id="inst_a")
    successor, created = service.attach(ws, instance_id="inst_b")
    assert created is True
    assert successor.leader_epoch == lease.leader_epoch + 1
    conn.close()


def test_require_refuses_missing_token_and_wrong_instance(tmp_path: Path) -> None:
    conn, store, ws = _workspace(tmp_path)
    service = LeaderService(store)
    lease, _ = service.attach(ws, instance_id="inst_a")
    with pytest.raises(LeaderRequired):
        service.require(ws, None)
    with pytest.raises(LeaderConflict):
        service.require(ws, lease.lease_token, instance_id="someone_else")
    # A worker presenting no token never gets authority.
    with pytest.raises(LeaderRequired):
        service.require(ws, "")
    conn.close()


def test_workspaces_are_isolated(tmp_path: Path) -> None:
    conn, store, ws_a = _workspace(tmp_path, "a")
    root_b = tmp_path / "b"
    root_b.mkdir()
    ws_b, _ = WorkspaceResolver(store).get_or_create(str(root_b.resolve()))
    service = LeaderService(store)
    lease_a, _ = service.attach(ws_a, instance_id="inst_a")
    lease_b, _ = service.attach(ws_b, instance_id="inst_b")
    assert lease_a.leader_epoch == 1 and lease_b.leader_epoch == 1
    # A's token is meaningless for B.
    with pytest.raises(LeaderConflict):
        service.require(ws_b, lease_a.lease_token)
    with pytest.raises(LeaderConflict):
        service.require(ws_a, lease_b.lease_token)
    conn.close()


def test_concurrent_claims_produce_exactly_one_winner(tmp_path: Path) -> None:
    conn, store, ws = _workspace(tmp_path)
    conn.close()
    db_path = tmp_path / "registry.db"
    barrier = threading.Barrier(6)
    results: list[str] = []
    lock = threading.Lock()

    def claim(index: int) -> None:
        connection = open_connection(db_path)
        try:
            local_store = Store(connection)
            local_ws = local_store.workspaces.find_by_id(ws.id)
            barrier.wait()
            try:
                LeaderService(local_store).attach(local_ws, instance_id=f"inst_{index}")
                with lock:
                    results.append(f"won:{index}")
            except LeaderConflict:
                with lock:
                    results.append(f"lost:{index}")
        finally:
            connection.close()

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len([r for r in results if r.startswith("won")]) == 1, results
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM leader_leases").fetchone()[0] == 1


def test_validate_instance_id() -> None:
    assert validate_instance_id("abc").strip() == "abc"
    assert validate_instance_id(None).startswith("inst_")
    with pytest.raises(UsageError):
        validate_instance_id("   ")
    with pytest.raises(UsageError):
        validate_instance_id("x" * 129)


def test_status_reports_pending_inbox(tmp_path: Path) -> None:
    conn, store, ws = _workspace(tmp_path)
    service = LeaderService(store)
    service.attach(ws, instance_id="inst_a")
    status = service.status(ws)
    assert status.pending_inbox == 0
    assert status.held is True
    assert status.expires_in is not None and status.expires_in > 0
    body = status.to_dict()
    assert body["lease"]["leader_epoch"] == 1
    conn.close()
