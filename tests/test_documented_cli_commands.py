from pathlib import Path
import os
import subprocess
import sys

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
