"""Agent catalog: config overrides + measured definitions + local detection."""

from __future__ import annotations

from agentctl.agents import detector
from agentctl.agents.definitions import AGENT_DEFINITIONS, AgentDefinition
from agentctl.config.schema import Config
from agentctl.errors import (
    AgentDisabled,
    AgentNotInstalled,
    AgentUnknown,
)
from agentctl.storage.models import AgentSpec


def list_specs(config: Config, *, include_unconfigured: bool = False) -> list[AgentSpec]:
    """Catalog entries the user configured, enriched with measured facts."""
    specs: list[AgentSpec] = []
    ids = sorted(config.agents) if not include_unconfigured else sorted(
        set(config.agents) | set(AGENT_DEFINITIONS)
    )
    for agent_id in ids:
        entry = config.agents.get(agent_id)
        definition = AGENT_DEFINITIONS.get(agent_id)
        command = list(entry.command) if entry else list(definition.command) if definition else []
        enabled = bool(entry.enabled) if entry else False
        specs.append(
            AgentSpec(
                id=agent_id,
                command=command,
                enabled=enabled,
                supports_native_session=bool(definition and definition.session_id != "none"),
                supports_resume=bool(definition and definition.supports_resume),
                integration=bool(definition and definition.integration),
                notes=definition.notes if definition else "user-defined agent; capabilities unknown",
                extra={
                    "installed": detector.installed_command(command) if command else False,
                    "known": definition is not None,
                },
            )
        )
    return specs


def resolve(config: Config, agent_id: str) -> AgentSpec:
    """Validate that `agent_id` may be started, and return its spec."""
    entry = config.agents.get(agent_id)
    definition = AGENT_DEFINITIONS.get(agent_id)
    if entry is None and definition is None:
        raise AgentUnknown(
            f"agent {agent_id!r} is not in the catalog",
            "run `agentctl agents list`; add it under [agents.<id>] in config.toml",
        )
    if entry is not None and not entry.enabled:
        raise AgentDisabled(
            f"agent {agent_id!r} is disabled in config.toml",
            f"set [agents.{agent_id}] enabled = true",
        )
    if definition is None:
        raise AgentUnknown(
            f"agent {agent_id!r} is configured but has no verified Herdr Core integration",
            "only catalog agents from agentctl's definitions can be launched",
        )
    command = list(entry.command) if entry else list(definition.command)
    if not detector.installed_command(command):
        raise AgentNotInstalled(
            f"agent {agent_id!r} is configured but `{command[0]}` is not on PATH",
            "install the agent, fix its command in config.toml, or disable it",
        )
    return AgentSpec(
        id=definition.id,
        command=command,
        enabled=True,
        supports_native_session=definition.session_id != "none",
        supports_resume=definition.supports_resume,
        integration=definition.integration,
        notes=definition.notes,
        extra={"installed": True, "known": True, "definition": definition},
    )


def definition_for(agent_id: str) -> AgentDefinition:
    definition = AGENT_DEFINITIONS.get(agent_id)
    if definition is None:
        raise AgentUnknown(f"agent {agent_id!r} has no verified integration")
    return definition


def version_verified(definition: AgentDefinition, installed_version: str | None) -> bool | None:
    """Whether the installed version matches the one capabilities were measured on.

    `None` means there is nothing to compare: no measured version or the binary
    did not report one. A mismatch is a warning, never a refusal: agents update
    on their own schedule.
    """
    if not definition.verified_with or not installed_version:
        return None
    return definition.verified_with in installed_version
