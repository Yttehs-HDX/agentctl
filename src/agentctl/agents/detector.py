"""Agent details that need the local machine: is the command installed?"""

from __future__ import annotations

import shutil
import subprocess

from agentctl.agents.definitions import AgentDefinition


def installed_command(command: list[str]) -> bool:
    """Whether the executable a spec will actually launch is on PATH."""
    return bool(command) and shutil.which(command[0]) is not None


def installed(definition: AgentDefinition) -> bool:
    return installed_command(definition.command)


def version(definition: AgentDefinition) -> str | None:
    executable = shutil.which(definition.command[0])
    if executable is None:
        return None
    try:
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or proc.stderr).strip().splitlines()
    return text[0].strip() if text else None
