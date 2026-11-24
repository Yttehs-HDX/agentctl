"""Built-in agent definitions.

Capabilities here are *measured*, not inferred from the program name. Each
entry records how it was verified against hc 0.1.18 on 2026-11-24; see
docs/runtime-capabilities.md for the raw evidence.

Fields:
  integration        the agent emits HC status records (OSC 7501), so
                     idle/working/done/blocked/error are trustworthy.
  session_id         how agentctl learns the agent's own session id:
                       "explicit"   - agentctl supplies a UUID at launch
                       "session_dir"- agentctl supplies a private session
                                      directory and reads the id from the file
                                      the agent writes there
                       "none"       - not discoverable; stays NULL
  supports_resume    the agent can be relaunched into an existing session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SessionIdStrategy = Literal["explicit", "session_dir", "none"]


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    id: str
    command: list[str]
    integration: bool
    session_id: SessionIdStrategy
    supports_resume: bool
    # Flag that carries a caller-chosen session UUID ("explicit" strategy).
    session_id_flag: str | None = None
    # Flag that points the agent at a private session directory.
    session_dir_flag: str | None = None
    # Flag that loads a stored session by id. Bare picker-opening forms are not
    # used: `pi --resume` without an id opens a picker, `pi --session <id>` loads.
    resume_flag: str | None = None
    notes: str = ""
    resume_verified: bool = False
    # Version this entry's capabilities were measured against. `None` means the
    # capability was not tied to a specific release.
    verified_with: str | None = None
    extra_args: list[str] = field(default_factory=list)


AGENT_DEFINITIONS: dict[str, AgentDefinition] = {
    "omp": AgentDefinition(
        id="omp",
        command=["omp"],
        integration=True,
        session_id="session_dir",
        supports_resume=True,
        session_dir_flag="--session-dir",
        resume_flag="--resume",
        resume_verified=True,
        verified_with="19.1.0",
        notes="reports idle/working/done; --resume <id> restores context (verified)",
    ),
    "pi": AgentDefinition(
        id="pi",
        command=["pi"],
        integration=True,
        session_id="explicit",
        session_id_flag="--session-id",
        session_dir_flag="--session-dir",
        # pi --resume (bare) opens a picker; --session <id> loads directly.
        resume_flag="--session",
        supports_resume=True,
        resume_verified=True,
        notes="accepts a caller-chosen --session-id; --session <id> reloads it",
    ),
    "claude": AgentDefinition(
        id="claude",
        command=["claude"],
        integration=True,
        session_id="explicit",
        session_id_flag="--session-id",
        resume_flag="--resume",
        supports_resume=True,
        resume_verified=True,
        verified_with="2.1.327",
        notes="--session-id <uuid> is the stored session; first launch may block on a trust prompt",
    ),
    "codex": AgentDefinition(
        id="codex",
        command=["codex"],
        integration=False,
        session_id="none",
        supports_resume=False,
        verified_with="0.146.0",
        notes="does not emit HC status records and offers no way to pin a session id at launch; "
        "status stays unknown and native ids are not captured in v0.1",
    ),
    "opencode": AgentDefinition(
        id="opencode",
        command=["opencode"],
        integration=False,
        session_id="none",
        supports_resume=False,
        notes="no verified Herdr Core integration on this machine",
    ),
}
