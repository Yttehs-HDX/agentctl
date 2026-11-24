"""The generated hook must be valid JavaScript.

The hook is written by a Python template, so an escape can silently corrupt the
JS and the harness then refuses to load the extension. That failure is invisible
from the worker's side, so it is caught here instead.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from agentctl.integrations.omp_hook import omp_hook, pi_hook

node = shutil.which("node")


def _render(**kwargs) -> str:
    return omp_hook(*kwargs.values()) if kwargs else omp_hook("/spool/omp.jsonl")


def test_generated_hooks_carry_no_raw_newline_in_a_string() -> None:
    for source in (omp_hook("/spool/omp.jsonl"), pi_hook("/spool/pi.jsonl")):
        for number, line in enumerate(source.splitlines(), 1):
            assert line.count('"') % 2 == 0 or "+" in line, f"unbalanced quote line {number}: {line}"


def test_generated_regexes_are_single_escaped() -> None:
    source = omp_hook("/spool/omp.jsonl")
    # A doubled backslash inside the regex literal would never match a message id.
    assert r"/\[\[agentctl:(msg_[A-Za-z0-9_-]+)\]\]/" in source
    assert r"/\\s+/g" not in source
    assert r"/\s+/g" in source


def test_generated_hook_passes_the_harness_loader(tmp_path, monkeypatch) -> None:
    """The only parser that matters is the one that will load the file.

    Measured: a broken escape makes the harness print
    `Failed to load extension ... Failed to parse extension source` and continue,
    which looks exactly like a hook that never fires. Loading the real file with
    the real binary is the check that catches that class of bug.
    """
    if shutil.which("omp") is None:
        pytest.skip("omp is not installed")
    source = omp_hook(str(tmp_path / "spool" / "omp.jsonl"))
    path = tmp_path / "agentctl.ts"
    path.write_text(source)
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "AGENTCTL_WORKSPACE_ID": "ws_probe",
        "AGENTCTL_SESSION_ID": "sess_probe",
    }
    proc = subprocess.run(
        ["omp", "-e", str(path), "-p", "--no-session", "--no-tools", "--offline",
         "--model", "none", "x"],
        capture_output=True, text=True, timeout=120, env=env,
        cwd=str(tmp_path),
    )
    combined = proc.stdout + proc.stderr
    assert "Failed to parse extension source" not in combined, combined[-2000:]
    assert "Failed to load extension" not in combined, combined[-2000:]
    # Loading runs the factory, which spools one record.
    spool = tmp_path / "spool" / "omp.jsonl"
    assert spool.exists(), "a loaded hook must spool its session record"
    import json

    record = json.loads(spool.read_text().splitlines()[0])
    assert record["kind"] == "session_started"
