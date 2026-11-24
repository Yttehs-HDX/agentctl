"""Reliable message registry."""

from agentctl.messaging.transitions import ACTIVE_STATES, ALLOWED, can_move, describe, is_terminal

__all__ = ["ACTIVE_STATES", "ALLOWED", "can_move", "describe", "is_terminal"]
