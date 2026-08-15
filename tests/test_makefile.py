from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]


def assert_target_renders_valid_shell(target: str) -> None:
    planned = subprocess.run(
        ["make", "-n", target],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    syntax = subprocess.run(
        ["/bin/sh", "-n"],
        input=planned.stdout,
        capture_output=True,
        text=True,
    )

    assert syntax.returncode == 0, syntax.stderr


def test_pr_commit_recipe_renders_valid_shell() -> None:
    assert_target_renders_valid_shell("pr-commit")


def test_pr_push_recipe_renders_valid_shell() -> None:
    assert_target_renders_valid_shell("pr-push")


def test_docs_target_validates_tracked_documentation() -> None:
    result = subprocess.run(
        ["make", "docs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_release_readiness_does_not_report_no_todos_as_a_warning() -> None:
    result = subprocess.run(
        ["make", "check-release-ready"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Found issues: none" not in result.stdout


def test_version_target_reads_the_project_version() -> None:
    result = subprocess.run(
        ["make", "version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "=== Version: 0.2.0 ===" in result.stdout
