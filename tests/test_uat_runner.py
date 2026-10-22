"""Contract tests for the isolated UAT root launcher."""

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "uat-run.sh"


def run_uat(tmp_path, *args, root=None):
    home = tmp_path / "home"
    home.mkdir()
    environment = {"HOME": str(home), "PATH": os.environ["PATH"]}
    if root is not None:
        environment["DROVER_UAT_ROOT"] = str(root)
    return (
        subprocess.run(
            [str(SCRIPT), *args],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        ),
        home,
    )


def test_uat_runner_creates_private_default_root_and_scopes_child_environment(tmp_path):
    result, home = run_uat(
        tmp_path,
        "--",
        "sh",
        "-c",
        'printf "%s" "$DROVER_FOLDER_ROOTS"',
    )
    root = home / "drover-uat"
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(root)
    assert root.is_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_uat_runner_refuses_root_outside_home(tmp_path):
    outside = tmp_path / "outside"
    result, _ = run_uat(tmp_path, "--", "true", root=outside)
    assert result.returncode == 2
    assert "absolute descendant of HOME" in result.stderr
    assert not outside.exists()


def test_uat_runner_refuses_symlink_root(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    root = home / "drover-uat"
    root.symlink_to(target, target_is_directory=True)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "DROVER_UAT_ROOT": str(root),
    }
    result = subprocess.run(
        [str(SCRIPT), "--", "true"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "must not be a symlink" in result.stderr
