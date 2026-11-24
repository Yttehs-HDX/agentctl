"""Leader lease: which process may act for this workspace right now?

The model
---------
`workspace_id` and `leader_id` are durable identities created by `init`. They
never change and they are not held by anyone in particular. Authority comes
from a *lease*: one row per workspace naming exactly one `leader_instance_id`,
the `leader_epoch` it took over at, and a `lease_token` it must present for
every mutating call.

Why a lease and not a pid/lock file
-----------------------------------
The Leader is often a chat session, so any process claiming authority must be
able to lose it when it dies without cleanup. A row with an expiry covers that:
a crashed Leader stops renewing, the row expires, and the next claim succeeds.
The row is the arbiter because SQLite serializes writers on it, so two claims
racing each other produce one winner and one clear conflict, with no window
where both believe they hold it.

Fencing
-------
Every takeover bumps `leader_epoch`. A control call presents its token, and the
service compares `(epoch, token, instance)` against the stored row. An old
instance that was superseded keeps failing even though its row write succeeded
earlier, because the stored epoch moved on. That is the fencing property: a
stale Leader cannot execute a side-effecting command.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentctl.clock import elapsed_since, new_id, now_precise, plus, token
from agentctl.errors import LeaderConflict, LeaderRequired, UsageError
from agentctl.storage.database import transaction
from agentctl.storage.models import LeaderLease, Workspace

# How long a lease stays valid without a heartbeat. Long enough that a Leader
# thinking hard through one long turn does not lose authority, short enough
# that a crashed Leader is replaceable without a manual repair step.
DEFAULT_LEASE_SECONDS = 120.0
# A leader instance must be able to state its own identity. A per-process value
# is right: two CLI invocations from the same process share it deliberately
# (they are the same Leader), while a new process must claim for itself.
INSTANCE_ENV = "AGENTCTL_LEADER_INSTANCE"


@dataclass(slots=True)
class LeaderStatus:
    workspace: Workspace
    lease: LeaderLease | None
    held: bool
    expired: bool
    expires_in: float | None
    pending_inbox: int

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace.id,
            "leader_id": self.workspace.leader_id,
            "held": self.held,
            "expired": self.expired,
            "expires_in_seconds": None if self.expires_in is None else round(self.expires_in, 1),
            "lease": self.lease.to_dict() if self.lease else None,
            "inbox_pending": self.pending_inbox,
        }


class LeaderService:
    """Claim, renew, release and fence the workspace lease.

    Every method that mutates takes the write lock through `transaction`, so
    the read of the current row and the decision about it are one atomic step.
    """

    def __init__(self, store, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
        self.store = store
        self.lease_seconds = lease_seconds

    # ------------------------------------------------------------- inspection

    def current(self, workspace: Workspace) -> LeaderLease | None:
        return self.store.leases.get(workspace.id)

    def _expired(self, lease: LeaderLease | None) -> bool:
        if lease is None or not lease.held:
            return True
        remaining = elapsed_since(lease.expires_at)
        return remaining is None or remaining >= 0

    def status(self, workspace: Workspace) -> LeaderStatus:
        lease = self.current(workspace)
        expired = self._expired(lease)
        expires_in: float | None = None
        if lease is not None and not expired:
            remaining = elapsed_since(lease.expires_at)
            expires_in = None if remaining is None else -remaining
        return LeaderStatus(
            workspace=workspace,
            lease=lease,
            held=lease is not None and lease.held and not expired,
            expired=expired and lease is not None,
            expires_in=expires_in,
            pending_inbox=self.store.inbox.count_pending(workspace.id),
        )

    # ------------------------------------------------------------------ claim

    def attach(
        self,
        workspace: Workspace,
        *,
        instance_id: str | None = None,
        takeover: bool = False,
        note: str | None = None,
    ) -> tuple[LeaderLease, bool]:
        """Claim the lease. Returns (lease, created) where created means new.

        `takeover` is the explicit "I know someone else holds this" path. It is
        still refused while a *live* lease exists unless `force` semantics are
        requested through `takeover`, because silently stealing authority from
        a running Leader is exactly what the lease exists to prevent.
        """
        instance = instance_id or new_id("inst")
        stamp = now_precise()
        with transaction(self.store.conn):
            existing = self.store.leases.get(workspace.id)
            # Re-attaching the same instance is a renewal, not a takeover, so a
            # Leader calling `attach` twice does not bump its own epoch.
            if existing is not None and existing.held and not self._expired(existing):
                if existing.leader_instance_id == instance:
                    renewed = self._renew_row(existing, stamp)
                    return renewed, False
                if not takeover:
                    raise LeaderConflict(
                        f"workspace {workspace.id} already has a live Leader "
                        f"(instance {existing.leader_instance_id}, epoch {existing.leader_epoch})",
                        "if that Leader is gone, use `agentctl leader takeover`; "
                        "otherwise run `agentctl leader status`",
                    )
            epoch = 1 if existing is None else existing.leader_epoch + 1
            lease = LeaderLease(
                workspace_id=workspace.id,
                leader_id=workspace.leader_id,
                leader_instance_id=instance,
                leader_epoch=epoch,
                lease_token=token(),
                acquired_at=stamp,
                renewed_at=stamp,
                expires_at=plus(stamp, self.lease_seconds),
                released_at=None,
            )
            self.store.leases.upsert(lease)
        return lease, True

    def _renew_row(self, lease: LeaderLease, stamp: str) -> LeaderLease:
        if not self.store.leases.renew(
            lease.workspace_id,
            lease.lease_token,
            renewed_at=stamp,
            expires_at=plus(stamp, self.lease_seconds),
        ):
            raise LeaderConflict(
                "lease changed while renewing",
                "run `agentctl leader status`, then re-attach",
            )
        return LeaderLease(
            workspace_id=lease.workspace_id,
            leader_id=lease.leader_id,
            leader_instance_id=lease.leader_instance_id,
            leader_epoch=lease.leader_epoch,
            lease_token=lease.lease_token,
            acquired_at=lease.acquired_at,
            renewed_at=stamp,
            expires_at=plus(stamp, self.lease_seconds),
            released_at=None,
        )

    def takeover(self, workspace: Workspace, *, instance_id: str | None = None) -> LeaderLease:
        lease, _created = self.attach(workspace, instance_id=instance_id, takeover=True)
        return lease

    def renew(self, workspace: Workspace, lease_token: str, *, instance_id: str) -> LeaderLease:
        stamp = now_precise()
        self.require(workspace, lease_token, instance_id=instance_id)
        with transaction(self.store.conn):
            existing = self.store.leases.get(workspace.id)
            if existing is None or existing.lease_token != lease_token:
                raise LeaderConflict(
                    "the lease was taken over by another Leader instance",
                    "run `agentctl leader status`, then attach or takeover",
                )
            return self._renew_row(existing, stamp)

    def release(self, workspace: Workspace, lease_token: str, *, instance_id: str) -> bool:
        self.require(workspace, lease_token, instance_id=instance_id)
        with transaction(self.store.conn):
            return self.store.leases.release(workspace.id, lease_token, released_at=now_precise())

    def authorize(self, workspace: Workspace, lease_token: str | None, *, instance_id: str | None = None) -> str:
        """Decide whether a mutating command may proceed. Returns the authority kind.

        "leader" once a live lease exists and the caller presents its token;
        "unclaimed" when no Leader has ever attached, which keeps single-operator
        v0.1 use working; and an error in every other case. Fencing lives here:
        the moment someone attaches or takes over, an older instance's token
        stops being accepted.
        """
        lease = self.current(workspace)
        if lease is None:
            return "unclaimed"
        if not lease_token:
            if not lease.held:
                return "unclaimed"
            raise LeaderRequired(
                f"workspace {workspace.id} has a live Leader (epoch {lease.leader_epoch})",
                "pass --lease-token or set AGENTCTL_LEASE_TOKEN from `agentctl leader attach`; "
                "or run `agentctl leader takeover` to replace it",
            )
        self.require(workspace, lease_token, instance_id=instance_id)
        return "leader"

    # ---------------------------------------------------------------- fencing

    def require(
        self,
        workspace: Workspace,
        lease_token: str | None,
        *,
        instance_id: str | None = None,
        require_live: bool = True,
    ) -> LeaderLease:
        """Raise unless `lease_token` is the workspace's current, live lease.

        This is the single gate every mutating command calls. It is
        deliberately strict about identity: a token alone is not enough once
        an operator has taken over, because the token of a superseded epoch must
        stop working.
        """
        if not lease_token:
            raise LeaderRequired(
                f"workspace {workspace.id} has no Leader lease presented",
                "run `agentctl leader attach` and pass its lease token, "
                "or set AGENTCTL_LEASE_TOKEN",
            )
        lease = self.current(workspace)
        if lease is None:
            raise LeaderRequired(
                f"workspace {workspace.id} has no Leader lease",
                "run `agentctl leader attach` first",
            )
        if lease.lease_token != lease_token:
            raise LeaderConflict(
                f"lease token does not match the current Leader "
                f"(held by instance {lease.leader_instance_id}, epoch {lease.leader_epoch})",
                "another Leader took over; run `agentctl leader status` to see who holds it",
            )
        if instance_id is not None and lease.leader_instance_id != instance_id:
            raise LeaderConflict(
                "this Leader instance was superseded by another instance",
                "run `agentctl leader status`, then re-attach",
            )
        if not lease.held:
            raise LeaderConflict(
                "the Leader lease was released",
                "run `agentctl leader attach` to claim it again",
            )
        if require_live and self._expired(lease):
            raise LeaderConflict(
                "the Leader lease expired",
                "run `agentctl leader heartbeat` to renew it, or `agentctl leader attach`",
            )
        return lease


def validate_instance_id(value: str | None) -> str:
    if value is None or value == "":
        return new_id("inst")
    cleaned = value.strip()
    if not cleaned:
        raise UsageError("leader instance id must not be blank")
    if len(cleaned) > 128:
        raise UsageError("leader instance id must be at most 128 characters")
    return cleaned
