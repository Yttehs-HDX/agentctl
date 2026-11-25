"""The bundled Leader skill: discovery, install targets, and CLI wiring."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentctl import skills
from agentctl.cli import app

runner = CliRunner()


def _frontmatter(text: str) -> dict[str, str]:
    _, front, _rest = text.split("---", 2)
    fields: dict[str, str] = {}
    for line in front.strip().splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


# ------------------------------------------------------------------ source_dir


def test_source_dir_ships_the_leader_skill() -> None:
    path = skills.source_dir()
    assert path.name == skills.SKILL_ID
    skill_file = path / "SKILL.md"
    assert skill_file.is_file()
    assert _frontmatter(skill_file.read_text())["name"] == "agentctl-leader"


# ---------------------------------------------------------------------- install


def _target(base: Path) -> Path:
    return base / ".agents" / "skills" / skills.SKILL_ID


@pytest.fixture
def source() -> Path:
    return skills.source_dir()


@pytest.mark.skipif(os.name == "nt", reason="Windows installs a copy")
def test_install_links_then_is_unchanged(tmp_path, source) -> None:
    path = _target(tmp_path)

    assert skills.install(path, source).action == "created"
    assert path.is_symlink()
    assert (path / "SKILL.md").is_file()

    assert skills.install(path, source).action == "unchanged"
    assert path.is_symlink()


def test_install_copy_creates_a_directory(tmp_path, source) -> None:
    path = _target(tmp_path)

    assert skills.install(path, source, copy=True).action == "created"
    assert path.is_dir() and not path.is_symlink()
    assert (path / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()


def test_install_refuses_foreign_directory_then_force_replaces_it(tmp_path, source) -> None:
    path = _target(tmp_path)
    path.mkdir(parents=True)
    (path / "notes.txt").write_text("someone else's skill")

    refused = skills.install(path, source)
    assert refused.action == "skipped"
    assert refused.error is not None and str(path) in refused.error
    assert (path / "notes.txt").is_file()

    assert skills.install(path, source, force=True).action == "replaced"
    assert (path / "SKILL.md").is_file()
    assert not (path / "notes.txt").exists()


# -------------------------------------------------------------------- uninstall


@pytest.mark.skipif(os.name == "nt", reason="Windows installs a copy")
def test_uninstall_removes_a_link(tmp_path, source) -> None:
    path = _target(tmp_path)
    skills.install(path, source)

    assert skills.uninstall(path, source).action == "removed"
    assert not path.exists() and not path.is_symlink()


def test_uninstall_removes_a_copy(tmp_path, source) -> None:
    path = _target(tmp_path)
    skills.install(path, source, copy=True)

    assert skills.uninstall(path, source).action == "removed"
    assert not path.exists()


def test_uninstall_refuses_a_foreign_directory(tmp_path, source) -> None:
    path = _target(tmp_path)
    path.mkdir(parents=True)
    (path / "other.txt").write_text("mine")

    refused = skills.uninstall(path, source)
    assert refused.action == "skipped"
    assert refused.error is not None and str(path) in refused.error
    assert (path / "other.txt").is_file()


def test_status_reports_each_state(tmp_path, source) -> None:
    missing = _target(tmp_path / "missing")
    default = _target(tmp_path / "default")
    copied = _target(tmp_path / "copied")
    foreign = _target(tmp_path / "foreign")
    foreign.mkdir(parents=True)
    (foreign / "notes.txt").write_text("mine")

    skills.install(default, source)
    skills.install(copied, source, copy=True)

    assert skills.status(missing, source) == "missing"
    assert skills.status(default, source) == ("copied" if os.name == "nt" else "linked")
    assert skills.status(copied, source) == "copied"
    assert skills.status(foreign, source) == "foreign"


# -------------------------------------------------------------------------- CLI


def test_cli_path_reports_an_existing_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    result = runner.invoke(app, ["--json", "skills", "path"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["skill"] == skills.SKILL_ID
    assert (Path(body["source"]) / "SKILL.md").is_file()


@pytest.mark.parametrize(
    ("flags", "scope", "root"),
    [([], "user", "home"), (["--user"], "user", "home"), (["--project"], "project", "project")],
)
def test_cli_install_status_uninstall_round_trip(monkeypatch, tmp_path, flags, scope, root) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OMP_PROFILE", "work")
    target = tmp_path / root / ".agents" / "skills" / skills.SKILL_ID

    legacy_paths = [
        home / ".claude" / "skills" / skills.SKILL_ID,
        home / ".omp" / "agent" / "skills" / skills.SKILL_ID,
        home / ".omp" / "profiles" / "work" / "agent" / "skills" / skills.SKILL_ID,
        project / ".claude" / "skills" / skills.SKILL_ID,
        project / ".omp" / "skills" / skills.SKILL_ID,
    ]
    skill_contents = (skills.source_dir() / "SKILL.md").read_bytes()
    for path in legacy_paths:
        path.mkdir(parents=True)
        (path / "SKILL.md").write_bytes(skill_contents)

    install = runner.invoke(app, ["skills", "install", *flags])
    assert install.exit_code == 0, install.output
    assert (target / "SKILL.md").is_file()

    status = runner.invoke(app, ["--json", "skills", "status", *flags])
    assert status.exit_code == 0, status.output
    body = json.loads(status.stdout)
    assert body["skill"] == skills.SKILL_ID
    expected_state = "copied" if os.name == "nt" else "linked"
    assert body["targets"] == [{"scope": scope, "path": str(target), "state": expected_state}]

    uninstall = runner.invoke(app, ["skills", "uninstall", *flags])
    assert uninstall.exit_code == 0, uninstall.output
    assert not target.exists() and not target.is_symlink()
    for path in legacy_paths:
        assert not path.is_symlink()
        assert (path / "SKILL.md").read_bytes() == skill_contents


@pytest.mark.parametrize("command", ["install", "status", "uninstall"])
def test_skill_commands_reject_removed_harness_option(command) -> None:
    result = runner.invoke(app, ["skills", command, "--harness", "omp"])
    assert result.exit_code == 2, result.output
    assert "No such option" in result.output


# -------------------------------------------------- skill command validation

_GROUP_COMMANDS = {"leader", "agents", "config", "skills"}
_GLOBAL_WITH_VALUE = {"--workspace", "-w"}
_GLOBAL_FLAGS = {"--json"}


def _fenced_sh_blocks(markdown: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    buffer: list[str] = []
    fence: str | None = None
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if fence is None:
                fence = stripped[3:].strip()
                buffer = []
            else:
                if fence == "sh":
                    blocks.append(buffer)
                fence = None
            continue
        if fence is not None:
            buffer.append(line)
    return blocks


def _skill_command_lines(markdown: str) -> list[str]:
    lines: list[str] = []
    for block in _fenced_sh_blocks(markdown):
        for line in block:
            text = line.strip()
            if text.startswith("agentctl "):
                lines.append(text)
    return lines


def _parse_command(line: str) -> tuple[list[str], list[str]]:
    tokens = shlex.split(line)
    assert tokens[0] == "agentctl", line
    rest = tokens[1:]
    if "--" in rest:
        rest = rest[: rest.index("--")]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in _GLOBAL_WITH_VALUE:
            index += 2
        elif token in _GLOBAL_FLAGS:
            index += 1
        else:
            break
    command = rest[index : index + (2 if rest[index] in _GROUP_COMMANDS else 1)]
    options = [token for token in rest if token.startswith("--")]
    return command, options


def test_every_skill_command_and_option_exists_in_the_cli() -> None:
    markdown = (skills.source_dir() / "SKILL.md").read_text()
    lines = _skill_command_lines(markdown)
    assert lines, "no fenced sh command lines found in the skill"

    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0, root.output

    for line in lines:
        command, options = _parse_command(line)
        result = runner.invoke(app, [*command, "--help"])
        assert result.exit_code == 0, f"{line}\n{result.output}"
        for option in options:
            if option in _GLOBAL_WITH_VALUE or option in _GLOBAL_FLAGS:
                assert option in root.stdout, f"{option} missing from root help ({line})"
            else:
                assert option in result.stdout, f"{option} missing from {' '.join(command)} help ({line})"
