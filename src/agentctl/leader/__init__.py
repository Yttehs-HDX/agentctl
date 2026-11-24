"""Leader binding: where the workspace's Leader is running."""

from agentctl.leader.service import (
    LEADER_ENV,
    LEADER_ROLE,
    ROLE_LABEL,
    WORKER_ROLE,
    LeaderService,
    LeaderStatus,
)

__all__ = [
    "LEADER_ENV",
    "LEADER_ROLE",
    "ROLE_LABEL",
    "WORKER_ROLE",
    "LeaderService",
    "LeaderStatus",
]
