import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_archive_docs_attribute_dry_run_to_the_installer() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    archive_section = readme.split("## Optional: cross-session recall archive", 1)[
        1
    ].split("## Context store", 1)[0]

    assert "`install.sh --dry-run`" in archive_section
    assert "Pass `--dry-run`" not in archive_section
