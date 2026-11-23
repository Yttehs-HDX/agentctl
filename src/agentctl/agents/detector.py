"""Agent details that need the local machine: is the command installed?"""

from __future__ import annotations

import shutil
import subprocess

from agentctl.agents.definitions import AgentDefinition


def installed(definition: AgentDefinition) -> bool:
    return shutil.which(definition.command[0]) is not None


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
