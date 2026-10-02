import os
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


def test_pond_archive_backup_cli_is_removed() -> None:
    # Phase 1 (#478) removed the Pond backup surface along with its runbook;
    # the remaining `archive` group only covers local inventories.
    root_context = click.Context(main)
    archive = main.get_command(root_context, "archive")
    assert isinstance(archive, click.Group)
    archive_context = click.Context(archive, parent=root_context)
    assert "backup" not in archive.list_commands(archive_context)
    assert not (ROOT / "docs" / "archive-r2-backup.md").exists()
