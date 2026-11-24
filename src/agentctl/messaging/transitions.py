"""Legal message transitions, in one place.

Splitting the state machine out of the service keeps the rules testable without
a database and stops each caller from inventing its own notion of "done". The
service asks `can_move()` before every write, so an illegal transition is a
typed error rather than a corrupted row.

    queued ──dispatch──▶ dispatching ──accepted──▶ dispatched ──ack──▶ acknowledged
      │                       │                        │                    │
      │                       └──refused──────────────┤                    ▼
      │                                                │                 running
      └──cancel────────────────────────────────────────┴──────────────▶ completed
                                                       │                    │
                                                       ▼                    ▼
                                              delivery_unknown          failed
"""

from __future__ import annotations

from agentctl.storage.models import MESSAGE_TERMINAL_STATES

# Every legal move. A state not listed here has no outgoing transitions, which
# is how "terminal" is expressed: completed/failed/cancelled appear only as
# values, never as keys.
ALLOWED: dict[str, frozenset[str]] = {
    "queued": frozenset({"dispatching", "dispatched", "cancelled", "failed"}),
    # A dispatch attempt may end in any of three ways: accepted, refused before
    # the text left, or unknown. The last one exists precisely because a crash
    # between "wrote to the terminal" and "recorded the write" is unknowable.
    "dispatching": frozenset(
        {"dispatched", "delivery_unknown", "queued", "failed", "cancelled"}
    ),
    "dispatched": frozenset({"acknowledged", "delivery_unknown", "completed", "failed", "cancelled"}),
    "acknowledged": frozenset({"running", "completed", "failed", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "delivery_unknown": frozenset({"acknowledged", "completed", "failed", "cancelled"}),
}


def can_move(current: str, target: str) -> bool:
    if current == target:
        return False
    return target in ALLOWED.get(current, frozenset())


def is_terminal(state: str) -> bool:
    return state in MESSAGE_TERMINAL_STATES


def describe(state: str) -> str:
    """One line an operator can act on, used in `messages get` output."""
    return {
        "queued": "persisted, not yet handed to the runtime",
        "dispatching": "being handed to the runtime right now",
        "dispatched": "accepted by the runtime transport, harness has not acknowledged",
        "acknowledged": "the worker harness confirmed it received this exact message",
        "running": "the worker began processing this message",
        "completed": "the worker's turn for this message ended",
        "failed": "delivery or execution failed; see last_error",
        "cancelled": "cancelled before completion",
        "delivery_unknown": "the text may have reached the worker; no ACK, never resend blindly",
    }.get(state, "unrecognised state")


# States where a message occupies its session's single in-flight slot. `queued`
# is excluded on purpose: a Leader may queue several tasks for one worker, and
# the worker runs them one after another.
INFLIGHT_STATES = (
    "dispatching",
    "dispatched",
    "acknowledged",
    "running",
    "delivery_unknown",
)

# Every state that has not finished, in-flight or waiting.
ACTIVE_STATES = ("queued", *INFLIGHT_STATES)
