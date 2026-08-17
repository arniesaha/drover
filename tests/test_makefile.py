import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def project_version() -> str:
    """The version the Makefile will report, read from the file it reads.

    Hardcoding it here meant every release bump broke this test after the
    fact, in CI, on the release branch itself. The assertion worth making is
    that `make version` agrees with pyproject.toml, not that either of them
    says a particular number.
    """

    match = re.search(
        r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.MULTILINE
    )
    assert match, "pyproject.toml has no version"
    return match.group(1)


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


def test_docs_target_checks_root_release_documents(tmp_path: Path) -> None:
    repo = make_release_repo(tmp_path)
    (repo / "SECURITY.md").write_text("[broken](missing-security-policy.md)\n")
    subprocess.run(["git", "add", "SECURITY.md"], cwd=repo, check=True)

    result = subprocess.run(
        ["make", "docs"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "SECURITY.md:1 -> missing-security-policy.md" in result.stdout


def test_phony_targets_are_implemented() -> None:
    makefile = (ROOT / "Makefile").read_text()
    phony = re.search(r"^\.PHONY: (.*)$", makefile, re.MULTILINE)

    assert phony is not None
    targets = set(re.findall(r"^([A-Za-z_-]+):", makefile, re.MULTILINE))
    assert set(phony.group(1).split()).issubset(targets)


def test_one_click_release_does_not_suggest_an_undefined_target() -> None:
    planned = subprocess.run(
        ["make", "-n", "example-one-click-release"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "release-upload" not in planned.stdout
    assert "Ready to publish the validated artifacts" in planned.stdout


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
    assert f"=== Version: {project_version()} ===" in result.stdout


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
    # check-release-ready requires the changelog to name the version being
    # released, so a fixture repo needs one to exercise anything past it.
    (repo / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{project_version()}] - 2026-01-01\n\n- seed\n"
    )
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


def test_release_readiness_rejects_a_changelog_that_omits_the_version(
    tmp_path: Path,
) -> None:
    # A release whose changelog does not mention it is a release nobody can
    # read the notes for, and it is the easiest thing to forget when the
    # version bump itself is a one-line diff.
    repo = make_release_repo(tmp_path / "no-entry")
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")

    result = subprocess.run(
        ["make", "check-release-ready"], cwd=repo, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "CHANGELOG.md has no" in result.stdout


def test_release_readiness_says_what_it_did_not_check(tmp_path: Path) -> None:
    # The gate passed on a morning when CI on main was red, and read as a
    # release sign-off because of its name. It cannot run the suite, but it
    # can decline to imply that it did.
    repo = make_release_repo(tmp_path / "honest")

    result = subprocess.run(
        ["make", "check-release-ready"], cwd=repo, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "does not run the" in result.stdout
    assert "pytest" in result.stdout
