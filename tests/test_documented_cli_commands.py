import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import click
import pytest

from drover.server.__main__ import main

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["credentials", "--help"], "revoke"),
        (["pair", "--help"], "pair"),
        (["pair-host", "--help"], "pair-host"),
        (["quality", "--help"], "quality"),
        (["observatory", "--help"], "observatory"),
        (["audit-sessions", "--help"], "audit-sessions"),
        (["setup-check", "--help"], "--host"),
    ],
)
def test_documented_cli_command_is_available(args: list[str], expected: str) -> None:
    environment = os.environ | {"PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "drover.server", *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout


def _documented_archive_backup_surface() -> dict[str, dict[str, tuple[bool, bool]]]:
    runbook = (ROOT / "docs" / "archive-r2-backup.md").read_text(encoding="utf-8")
    command_blocks = [
        block
        for block in re.findall(
            r"^[ \t]*```text[ \t]*\n(.*?)\n[ \t]*```[ \t]*$",
            runbook,
            flags=re.DOTALL | re.MULTILINE,
        )
        if "drover-server archive backup " in block
    ]
    assert len(command_blocks) == 1

    surface: dict[str, dict[str, tuple[bool, bool]]] = {}
    for line in command_blocks[0].splitlines():
        tokens = shlex.split(line)
        assert tokens[:3] == ["drover-server", "archive", "backup"]
        assert len(tokens) >= 4
        command = tokens[3]
        assert command not in surface
        options: dict[str, tuple[bool, bool]] = {}
        index = 4
        while index < len(tokens):
            documented = tokens[index]
            optional_flag = documented.startswith("[") and documented.endswith("]")
            option = documented[1:-1] if optional_flag else documented
            assert option.startswith("--")
            assert option not in options
            if optional_flag:
                options[option] = (False, True)
                index += 1
                continue
            assert index + 1 < len(tokens)
            assert re.fullmatch(r"<[^<>\s]+>", tokens[index + 1])
            options[option] = (True, False)
            index += 2
        surface[command] = options
    return surface


def _click_archive_backup_surface() -> dict[str, dict[str, tuple[bool, bool]]]:
    root_context = click.Context(main)
    archive = main.get_command(root_context, "archive")
    assert isinstance(archive, click.Group)
    archive_context = click.Context(archive, parent=root_context)
    backup = archive.get_command(archive_context, "backup")
    assert isinstance(backup, click.Group)
    backup_context = click.Context(backup, parent=archive_context)

    surface: dict[str, dict[str, tuple[bool, bool]]] = {}
    for command_name in backup.list_commands(backup_context):
        command = backup.get_command(backup_context, command_name)
        assert command is not None
        options: dict[str, tuple[bool, bool]] = {}
        for parameter in command.params:
            assert isinstance(parameter, click.Option)
            long_options = [
                option for option in parameter.opts if option.startswith("--")
            ]
            assert len(long_options) == 1
            option = long_options[0]
            assert option not in options
            options[option] = (parameter.required, parameter.is_flag)
        surface[command_name] = options
    return surface


def test_documented_archive_backup_surface_matches_click() -> None:
    assert _documented_archive_backup_surface() == _click_archive_backup_surface()


def test_archive_docs_attribute_dry_run_to_the_installer() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    archive_section = readme.split("## Optional: cross-session recall archive", 1)[
        1
    ].split("## Context store", 1)[0]

    assert "`install.sh --dry-run`" in archive_section
    assert "Pass `--dry-run`" not in archive_section
