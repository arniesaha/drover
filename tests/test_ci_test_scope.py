"""Behavioral tests for the fail-closed public-CI test selector."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

SELECTOR = Path(__file__).parents[1] / "scripts" / "ci" / "select_test_scope.py"


def select_scope(*paths: str) -> dict[str, bool]:
    result = subprocess.run(
        [sys.executable, str(SELECTOR), *paths],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "paths",
    [
        ("README.md",),
        ("README.md", "docs/assets/screenshots/ios-analytics.png"),
    ],
)
def test_presentation_only_paths_skip_expensive_suites(paths: tuple[str, ...]) -> None:
    assert select_scope(*paths) == {"ios": False, "python": False}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/drover/server/harness/daemon.py", {"ios": False, "python": True}),
        ("tests/test_harness_daemon.py", {"ios": False, "python": True}),
        ("apps/drover/DroverKit/Sources/App.swift", {"ios": True, "python": False}),
        (".github/workflows/ci.yml", {"ios": False, "python": True}),
        (".github/workflows/ios.yml", {"ios": True, "python": True}),
        ("install.sh", {"ios": False, "python": True}),
    ],
)
def test_runtime_paths_select_their_relevant_suite(
    path: str, expected: dict[str, bool]
) -> None:
    assert select_scope(path) == expected


def test_mixed_runtime_paths_select_both_suites() -> None:
    assert select_scope("src/drover/config.py", "apps/drover/project.yml") == {
        "ios": True,
        "python": True,
    }


def test_unknown_path_fails_closed_to_python() -> None:
    assert select_scope("deploy/new-runtime-unit.conf") == {
        "ios": False,
        "python": True,
    }


def test_selector_writes_github_step_outputs(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    result = subprocess.run(
        [
            sys.executable,
            str(SELECTOR),
            "--github-output",
            str(output),
            "README.md",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text() == "python=false\nios=false\n"
