from pathlib import Path
import shutil
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


def test_release_tag_rejects_a_command_line_tag_override(tmp_path: Path) -> None:
    marker = tmp_path / "injected"
    result = subprocess.run(
        ["make", "release-tag", f"TAG=v0.2.0; touch {marker}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "derives its tag from pyproject.toml" in result.stdout
    assert not marker.exists()


def make_release_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    shutil.copy2(ROOT / "Makefile", repo / "Makefile")
    shutil.copy2(ROOT / "pyproject.toml", repo / "pyproject.toml")
    (repo / "README.md").write_text("# Drover\n")
    shutil.copytree(ROOT / "scripts", repo / "scripts")
    (repo / "docs").mkdir()
    (repo / "docs" / "overview.md").write_text("# Overview\n")
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "test@drover.local"],
        ["git", "config", "user.name", "Drover Test"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    return repo


def test_release_readiness_allows_an_untagged_head_and_rejects_mismatch(
    tmp_path: Path,
) -> None:
    untagged = make_release_repo(tmp_path / "untagged")

    pending = subprocess.run(
        ["make", "check-release-ready"], cwd=untagged, capture_output=True, text=True
    )

    assert pending.returncode == 0, pending.stdout + pending.stderr
    assert "Release tag is pending" in pending.stdout

    mismatched = make_release_repo(tmp_path / "mismatched")
    subprocess.run(["git", "tag", "v0.2.1"], cwd=mismatched, check=True)
    result = subprocess.run(
        ["make", "check-release-ready"], cwd=mismatched, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "does not match pyproject.toml" in result.stdout
