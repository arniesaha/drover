"""Regression coverage for the release artifact PostgreSQL contract gate."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CHECKER = ROOT / "scripts" / "verify_release_candidate.py"


def _write_runtime_package(
    root: Path, *, has_control_store: bool, has_environment_file: bool
) -> None:
    package = root / "drover" / "server"
    package.mkdir(parents=True)
    (root / "drover" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    fields = (
        "    control_store: str = 'postgres'\n"
        if has_control_store
        else "    legacy: str = 'duckdb'\n"
    )
    (root / "drover" / "config.py").write_text(
        "from dataclasses import dataclass\n\n"
        "@dataclass\n"
        "class DroverConfig:\n"
        f"{fields}"
    )
    parameter = ", environment_file=None" if has_environment_file else ""
    (package / "service_units.py").write_text(
        f"def render_launchd(label{parameter}): pass\n"
        f"def render_systemd(label{parameter}): pass\n"
    )


def _write_server(root: Path) -> Path:
    server = root / "drover-server"
    server.write_text("#!/usr/bin/env sh\nexit 0\n")
    server.chmod(0o755)
    return server


@pytest.mark.parametrize(
    ("has_control_store", "has_environment_file"),
    [(False, True), (True, False)],
)
def test_candidate_gate_rejects_runtime_missing_installer_contract(
    tmp_path: Path, has_control_store: bool, has_environment_file: bool
) -> None:
    _write_runtime_package(
        tmp_path,
        has_control_store=has_control_store,
        has_environment_file=has_environment_file,
    )
    environment = os.environ | {"PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--python",
            sys.executable,
            "--server",
            str(_write_server(tmp_path)),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert (
        "candidate runtime lacks the PostgreSQL-default installer contract"
        in result.stderr
    )


def test_candidate_gate_accepts_runtime_with_full_installer_contract(
    tmp_path: Path,
) -> None:
    _write_runtime_package(tmp_path, has_control_store=True, has_environment_file=True)
    environment = os.environ | {"PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--python",
            sys.executable,
            "--server",
            str(_write_server(tmp_path)),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "satisfies the PostgreSQL-default installer contract" in result.stdout
