"""The bundled Leader skill: discovery, install targets, and CLI wiring."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

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


# ----------------------------------------------------------------- target_dirs


def test_project_targets_cover_every_harness(tmp_path) -> None:
    targets = skills.target_dirs("project", tmp_path, tmp_path / "home", {})
    assert [harness for harness, _scope, _path in targets] == ["omp", "claude", "agents"]
    paths = {harness: path for harness, _scope, path in targets}
    assert paths["omp"] == str(tmp_path / ".omp" / "skills" / skills.SKILL_ID)
    assert paths["claude"] == str(tmp_path / ".claude" / "skills" / skills.SKILL_ID)
    assert paths["agents"] == str(tmp_path / ".agents" / "skills" / skills.SKILL_ID)


def test_user_targets_omit_the_agents_layout(tmp_path) -> None:
    home = tmp_path / "home"
    targets = skills.target_dirs("user", tmp_path, home, {})
    harnesses = [harness for harness, _scope, _path in targets]
    assert harnesses == ["omp", "claude"]
    assert targets[0][2] == str(home / ".omp" / "agent" / "skills" / skills.SKILL_ID)


def test_user_targets_follow_the_omp_profile(tmp_path) -> None:
    home = tmp_path / "home"
    targets = skills.target_dirs("user", tmp_path, home, {"OMP_PROFILE": "work"})
    paths = {harness: path for harness, _scope, path in targets}
    expected = home / ".omp" / "profiles" / "work" / "agent" / "skills" / skills.SKILL_ID
    assert paths["omp"] == str(expected)
    assert "agents" not in paths


# ---------------------------------------------------------------------- install


def _target(tmp_path, harness: str = "omp") -> tuple[str, str, str]:
    return (harness, "project", str(tmp_path / "skills" / skills.SKILL_ID))


def test_install_links_then_is_unchanged(tmp_path) -> None:
    target = _target(tmp_path)
    path = tmp_path / "skills" / skills.SKILL_ID

    first = skills.install([target])
    assert first[0].action == "created"
    assert path.is_symlink()
    assert (path / "SKILL.md").is_file()

    second = skills.install([target])
    assert second[0].action == "unchanged"
    assert path.is_symlink()


def test_install_copy_creates_a_directory(tmp_path) -> None:
    target = _target(tmp_path)
    path = tmp_path / "skills" / skills.SKILL_ID

    rows = skills.install([target], copy=True)
    assert rows[0].action == "created"
    assert path.is_dir() and not path.is_symlink()
    assert (path / "SKILL.md").read_bytes() == (skills.source_dir() / "SKILL.md").read_bytes()


def test_install_refuses_foreign_directory_then_force_replaces_it(tmp_path) -> None:
    target = _target(tmp_path)
    path = tmp_path / "skills" / skills.SKILL_ID
    path.mkdir(parents=True)
    (path / "notes.txt").write_text("someone else's skill")

    refused = skills.install([target])
    assert refused[0].action == "skipped"
    assert refused[0].error is not None and str(path) in refused[0].error
    assert (path / "notes.txt").is_file()

    replaced = skills.install([target], force=True)
    assert replaced[0].action == "replaced"
    assert path.is_symlink()
    assert (path / "SKILL.md").is_file()
    assert not (path / "notes.txt").exists()


def test_one_refusal_does_not_stop_the_next_target(tmp_path) -> None:
    blocked = tmp_path / "skills" / skills.SKILL_ID
    blocked.mkdir(parents=True)
    (blocked / "notes.txt").write_text("foreign")
    clean = (tmp_path / "clean" / skills.SKILL_ID)

    rows = skills.install([_target(tmp_path), ("claude", "project", str(clean))])
    assert [row.action for row in rows] == ["skipped", "created"]
    assert (clean / "SKILL.md").is_file()


# -------------------------------------------------------------------- uninstall


def test_uninstall_removes_a_link(tmp_path) -> None:
    target = _target(tmp_path)
    path = tmp_path / "skills" / skills.SKILL_ID
    skills.install([target])

    rows = skills.uninstall([target])
    assert rows[0].action == "removed"
    assert not path.exists() and not path.is_symlink()


def test_uninstall_removes_a_copy(tmp_path) -> None:
    target = _target(tmp_path)
    path = tmp_path / "skills" / skills.SKILL_ID
    skills.install([target], copy=True)

    rows = skills.uninstall([target])
    assert rows[0].action == "removed"
    assert not path.exists()


def test_uninstall_refuses_a_foreign_directory(tmp_path) -> None:
    target = _target(tmp_path)
    path = tmp_path / "skills" / skills.SKILL_ID
    path.mkdir(parents=True)
    (path / "other.txt").write_text("mine")

    rows = skills.uninstall([target])
    assert rows[0].action == "skipped"
    assert rows[0].error is not None and str(path) in rows[0].error
    assert (path / "other.txt").is_file()


def test_status_reports_each_state(tmp_path) -> None:
    missing_target = ("a", "project", str(tmp_path / "missing" / skills.SKILL_ID))
    linked_target = ("b", "project", str(tmp_path / "linked" / skills.SKILL_ID))
    copied_target = ("c", "project", str(tmp_path / "copied" / skills.SKILL_ID))
    foreign_target = ("d", "project", str(tmp_path / "foreign" / skills.SKILL_ID))
    foreign_path = tmp_path / "foreign" / skills.SKILL_ID
    foreign_path.mkdir(parents=True)
    (foreign_path / "notes.txt").write_text("mine")

    skills.install([linked_target])
    skills.install([copied_target], copy=True)

    states = {
        row.harness: row.state
        for row in skills.status([missing_target, linked_target, copied_target, foreign_target])
    }
    assert states == {"a": "missing", "b": "linked", "c": "copied", "d": "foreign"}


# -------------------------------------------------------------------------- CLI


def test_cli_path_reports_an_existing_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    result = runner.invoke(app, ["--json", "skills", "path"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["skill"] == skills.SKILL_ID
    assert (Path(body["source"]) / "SKILL.md").is_file()


def test_cli_install_status_uninstall_round_trip(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    target = tmp_path / ".omp" / "skills" / skills.SKILL_ID

    install = runner.invoke(app, ["skills", "install", "--project", "--harness", "omp"])
    assert install.exit_code == 0, install.output
    assert (target / "SKILL.md").is_file()

    status = runner.invoke(app, ["--json", "skills", "status", "--project", "--harness", "omp"])
    assert status.exit_code == 0, status.output
    body = json.loads(status.stdout)
    assert body["skill"] == skills.SKILL_ID
    assert body["targets"] == [
        {"harness": "omp", "scope": "project", "path": str(target), "state": "linked"}
    ]

    uninstall = runner.invoke(app, ["skills", "uninstall", "--project", "--harness", "omp"])
    assert uninstall.exit_code == 0, uninstall.output
    assert not target.exists() and not target.is_symlink()


def test_cli_user_all_notes_the_missing_agents_layout(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    result = runner.invoke(app, ["--json", "skills", "status", "--user", "--harness", "all"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert [row["harness"] for row in body["targets"]] == ["omp", "claude"]
    assert "agents" in body["note"]


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
