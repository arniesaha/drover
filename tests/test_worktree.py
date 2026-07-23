"""Per-session git worktree lifecycle (see daemon structured-session flow).

Codex/Gemini structured sessions run full-auto with no approval channel, so
the daemon isolates each one in its own worktree: a broad ``git add -A``
inside the session can then never sweep unrelated in-flight changes from the
user's main checkout, and everything the session commits lands on a
``drover/<session-id>`` branch the user merges deliberately.
"""

from __future__ import annotations

import subprocess

import pytest

from drover.server.harness.worktree import (
    cleanup_session_worktree,
    create_session_worktree,
)


def _git(cwd, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "file.txt").write_text("hello\n")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-m", "initial")
    return root


def test_create_makes_worktree_on_session_branch(repo, tmp_path):
    worktrees_dir = tmp_path / "worktrees"
    wt = create_session_worktree(str(repo), "harness-abc", worktrees_dir)
    assert wt is not None
    assert wt.repo_root == str(repo.resolve())
    assert wt.branch == "drover/harness-abc"
    assert (worktrees_dir / "harness-abc" / "file.txt").is_file()
    assert _git(wt.path, "rev-parse", "--abbrev-ref", "HEAD") == "drover/harness-abc"
    assert wt.base_sha == _git(repo, "rev-parse", "HEAD")


def test_create_from_subdirectory_roots_at_toplevel(repo, tmp_path):
    sub = repo / "nested"
    sub.mkdir()
    wt = create_session_worktree(str(sub), "harness-sub", tmp_path / "worktrees")
    assert wt is not None
    assert wt.repo_root == str(repo.resolve())


def test_create_outside_git_repo_returns_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert create_session_worktree(str(plain), "harness-x", tmp_path / "wt") is None


def test_create_in_empty_repo_returns_none(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "init", "-b", "main")
    # No commits: there is no HEAD to base a worktree on.
    assert create_session_worktree(str(root), "harness-x", tmp_path / "wt") is None


def test_cleanup_removes_untouched_worktree_and_branch(repo, tmp_path):
    wt = create_session_worktree(str(repo), "harness-clean", tmp_path / "worktrees")
    assert cleanup_session_worktree(wt) == "removed"
    assert not (tmp_path / "worktrees" / "harness-clean").exists()
    branches = _git(repo, "branch", "--list", "drover/harness-clean")
    assert branches == ""


def test_cleanup_keeps_dirty_worktree(repo, tmp_path):
    wt = create_session_worktree(str(repo), "harness-dirty", tmp_path / "worktrees")
    (tmp_path / "worktrees" / "harness-dirty" / "wip.txt").write_text("wip\n")
    assert cleanup_session_worktree(wt) == "kept"
    assert (tmp_path / "worktrees" / "harness-dirty" / "wip.txt").is_file()


def test_cleanup_keeps_worktree_with_new_commits(repo, tmp_path):
    wt = create_session_worktree(str(repo), "harness-work", tmp_path / "worktrees")
    path = tmp_path / "worktrees" / "harness-work"
    (path / "done.txt").write_text("done\n")
    _git(path, "add", "done.txt")
    _git(path, "commit", "-m", "session work")
    assert cleanup_session_worktree(wt) == "kept"
    assert (path / "done.txt").is_file()
    assert _git(repo, "branch", "--list", "drover/harness-work") != ""


def test_cleanup_of_already_deleted_worktree_reports_missing(repo, tmp_path):
    wt = create_session_worktree(str(repo), "harness-gone", tmp_path / "worktrees")
    import shutil

    shutil.rmtree(tmp_path / "worktrees" / "harness-gone")
    assert cleanup_session_worktree(wt) == "missing"
    # The stale registration must not linger and block a future worktree at
    # the same path.
    assert "harness-gone" not in _git(wt.repo_root, "worktree", "list")
