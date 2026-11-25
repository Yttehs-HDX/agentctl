"""The CLI reference must have a section for every registered command.

`docs/cli.md` is what the README links as the full CLI reference, so a command
that ships without a section is a documentation bug rather than a missing
nicety. The list of commands comes from the typer app itself, never from a
hardcoded copy that would drift alongside it.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.main import get_command

from agentctl.cli import app

DOCS = Path(__file__).resolve().parents[1] / "docs" / "cli.md"


def test_every_registered_command_has_a_section() -> None:
    registered = set(get_command(app).commands)
    assert registered, "the typer app exposes no commands"

    headings = set(re.findall(r"^## (.+)$", DOCS.read_text(encoding="utf-8"), re.MULTILINE))
    missing = sorted(registered - headings)

    assert not missing, f"docs/cli.md has no section for: {', '.join(missing)}"
