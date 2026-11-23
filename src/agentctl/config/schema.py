"""Pydantic schema for ~/.config/agentctl/config.toml."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1


class GeneralSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION


class DefaultsSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Empty string means "no global default agent"; `None` is rejected so the
    # TOML stays explicit.
    agent: str = ""


class RuntimeSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Literal["herdr-core"] = "herdr-core"
    hc_command: str = "hc"
    call_timeout_seconds: float = Field(default=30.0, gt=0)


class WorkspaceSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path_policy: Literal["canonical"] = "canonical"


class SessionSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_mode: Literal["shared", "worktree"] = "shared"


class AgentEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: list[str]
    enabled: bool = True

    @field_validator("command")
    @classmethod
    def _non_empty(cls, value: list[str]) -> list[str]:
        if not value or not value[0]:
            raise ValueError("command must start with an executable")
        return value


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    general: GeneralSection = Field(default_factory=GeneralSection)
    defaults: DefaultsSection = Field(default_factory=DefaultsSection)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    workspace: WorkspaceSection = Field(default_factory=WorkspaceSection)
    session: SessionSection = Field(default_factory=SessionSection)
    agents: dict[str, AgentEntry] = Field(default_factory=dict)
