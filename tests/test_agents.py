"""Agent catalog: configured vs installed, unknown agents rejected."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentctl.agents.catalog import definition_for, list_specs, resolve
from agentctl.agents.definitions import AGENT_DEFINITIONS
from agentctl.config import default_config
from agentctl.errors import AgentDisabled, AgentNotInstalled, AgentUnknown


def test_every_configured_default_agent_has_a_definition() -> None:
    config = default_config()
    for agent_id in config.agents:
        assert agent_id in AGENT_DEFINITIONS


def test_list_specs_reports_installed_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentctl.agents.detector as detector

    monkeypatch.setattr(detector, "installed", lambda d: d.id == "omp")
    specs = {spec.id: spec for spec in list_specs(default_config())}
    assert specs["omp"].extra["installed"] is True
    assert specs["codex"].extra["installed"] is False


def test_unknown_agent_is_rejected() -> None:
    with pytest.raises(AgentUnknown):
        resolve(default_config(), "nope")


def test_disabled_agent_is_rejected() -> None:
    config = default_config()
    config.agents["omp"].enabled = False
    with pytest.raises(AgentDisabled):
        resolve(config, "omp")


def test_missing_binary_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentctl.agents.detector as detector

    monkeypatch.setattr(detector, "installed", lambda d: False)
    with pytest.raises(AgentNotInstalled):
        resolve(default_config(), "pi")


def test_resumable_agents_declare_a_flag() -> None:
    for agent_id, definition in AGENT_DEFINITIONS.items():
        if definition.supports_resume:
            assert definition.resume_flag, agent_id


def test_pi_resumes_with_session_not_resume() -> None:
    """`pi --resume` opens a picker and restores nothing; `--session <id>` loads.

    Measured live; a picker flag here would make recovery hang in a TUI.
    """
    assert definition_for("pi").resume_flag == "--session"


def test_capabilities_are_declared_per_agent() -> None:
    omp = definition_for("omp")
    codex = definition_for("codex")
    assert omp.integration is True
    assert omp.session_id != "none"
    assert omp.supports_resume is True
    # codex: measured as not reporting status and not pinnable at launch
    assert codex.integration is False
    assert codex.session_id == "none"


def test_agent_with_no_definition_cannot_be_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentctl.agents.detector as detector

    monkeypatch.setattr(detector, "installed", lambda d: True)
    config = default_config()
    from agentctl.config.schema import AgentEntry

    config.agents["my-agent"] = AgentEntry(command=["my-agent"], enabled=True)
    with pytest.raises(AgentUnknown):
        resolve(config, "my-agent")


def test_version_verified_compares_the_measured_release() -> None:
    from agentctl.agents.catalog import version_verified
    from agentctl.agents.definitions import AGENT_DEFINITIONS

    omp = AGENT_DEFINITIONS["omp"]
    assert version_verified(omp, "omp/19.1.0") is True
    assert version_verified(omp, "omp/19.1.1") is False
    assert version_verified(AGENT_DEFINITIONS["pi"], "1.5.2") is None
    assert version_verified(omp, None) is None
