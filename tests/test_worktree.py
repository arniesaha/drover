"""Per-session git worktree lifecycle (see daemon structured-session flow).

Codex/Gemini structured sessions run full-auto with no approval channel, so
the daemon isolates each one in its own worktree: a broad ``git add -A``
inside the session can then never sweep unrelated in-flight changes from the
user's main checkout, and everything the session commits lands on a
``drover/<session-id>`` branch the user merges deliberately.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from drover.server.harness.worktree import (
    WorktreeIsolationUnavailable,
    claim_worktrees_directory,
    cleanup_session_worktree,
    create_session_worktree,
    reclaim_stale_session_worktrees,
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


def test_worktrees_directory_has_one_daemon_owner(tmp_path):
    worktrees_dir = tmp_path / "worktrees"
    first = claim_worktrees_directory(worktrees_dir)
    sweep_copy = os.dup(first)
    try:
        with pytest.raises(WorktreeIsolationUnavailable, match="already in use"):
            claim_worktrees_directory(worktrees_dir)
    finally:
        os.close(first)

    try:
        with pytest.raises(WorktreeIsolationUnavailable, match="already in use"):
            claim_worktrees_directory(worktrees_dir)
    finally:
        os.close(sweep_copy)

    second = claim_worktrees_directory(worktrees_dir)
    os.close(second)


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


def test_cleanup_removes_branch_after_primary_checkout_changes(repo, tmp_path):
    wt = create_session_worktree(str(repo), "harness-clean", tmp_path / "worktrees")
    _git(repo, "checkout", "--orphan", "unrelated")
    (repo / "file.txt").unlink()
    (repo / "unrelated.txt").write_text("different history\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "unrelated root")

    assert cleanup_session_worktree(wt) == "removed"
    assert _git(repo, "branch", "--list", wt.branch) == ""


def test_cleanup_keeps_dirty_worktree(repo, tmp_path):
    wt = create_session_worktree(str(repo), "harness-dirty", tmp_path / "worktrees")
    (tmp_path / "worktrees" / "harness-dirty" / "wip.txt").write_text("wip\n")
    assert cleanup_session_worktree(wt) == "kept"
    assert (tmp_path / "worktrees" / "harness-dirty" / "wip.txt").is_file()


def test_cleanup_keeps_ignored_only_worktree_files(repo, tmp_path):
    """Ignored session output can still be the user's only copy."""
    (repo / ".gitignore").write_text("scratch.txt\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore scratch")
    wt = create_session_worktree(str(repo), "harness-ignored", tmp_path / "worktrees")
    assert wt is not None
    scratch = tmp_path / "worktrees" / "harness-ignored" / "scratch.txt"
    scratch.write_text("only copy\n")

    assert cleanup_session_worktree(wt) == "kept"
    assert scratch.read_text() == "only copy\n"


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


def test_git_timeout_raises_instead_of_silently_running_in_place(tmp_path, monkeypatch):
    """A failed worktree is not the same as a worktree being inapplicable.

    Both used to return None, so a transient git stall silently dropped the
    isolation that makes full-auto execution safe. Observed on the reference
    hub: `git worktree add` took 103s against 94 accumulated worktrees, blew
    the 15s timeout, and two codex sessions ran with --sandbox
    danger-full-access on main in the shared checkout.
    """
    from drover.server.harness.worktree import WorktreeIsolationUnavailable

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    real_run = subprocess.run

    def fail_on_worktree_add(cmd, *args, **kwargs):
        if "worktree" in cmd and "add" in cmd:
            raise subprocess.TimeoutExpired(cmd, 15)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_on_worktree_add)

    with pytest.raises(WorktreeIsolationUnavailable):
        create_session_worktree(str(repo), "harness-timeout", tmp_path / "worktrees")


def test_reclaim_removes_stale_clean_worktree(repo, tmp_path):
    """A clean worktree left by a prior run is reclaimed on the next start.

    The daemon's in-memory ``session_worktrees`` map is lost on restart, so a
    clean worktree created before a crash/restart is otherwise never cleaned up
    (#398). The sweep reconstructs the worktree and reuses the same
    "keep only if there is work" policy as session-end cleanup.
    """
    worktrees_dir = tmp_path / "worktrees"
    create_session_worktree(str(repo), "harness-stale", worktrees_dir)
    result = reclaim_stale_session_worktrees(worktrees_dir)
    assert result["removed"] == 1
    assert not (worktrees_dir / "harness-stale").exists()
    assert _git(repo, "branch", "--list", "drover/harness-stale") == ""


def test_reclaim_keeps_stale_dirty_worktree(repo, tmp_path):
    worktrees_dir = tmp_path / "worktrees"
    create_session_worktree(str(repo), "harness-dirty-stale", worktrees_dir)
    (worktrees_dir / "harness-dirty-stale" / "wip.txt").write_text("wip\n")
    result = reclaim_stale_session_worktrees(worktrees_dir)
    assert result["kept"] == 1
    assert (worktrees_dir / "harness-dirty-stale" / "wip.txt").is_file()


def test_reclaim_skips_worktrees_created_after_startup_snapshot(repo, tmp_path):
    """An asynchronous sweep must not remove a newly launched session."""
    worktrees_dir = tmp_path / "worktrees"
    create_session_worktree(str(repo), "harness-old", worktrees_dir)
    initial_paths = tuple(worktrees_dir.iterdir())
    create_session_worktree(str(repo), "harness-new", worktrees_dir)

    result = reclaim_stale_session_worktrees(worktrees_dir, candidates=initial_paths)

    assert result["removed"] == 1
    assert not (worktrees_dir / "harness-old").exists()
    assert (worktrees_dir / "harness-new").is_dir()


def test_reclaim_ignores_non_session_entries(repo, tmp_path):
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    (worktrees_dir / "notes.txt").write_text("not a worktree\n")
    result = reclaim_stale_session_worktrees(worktrees_dir)
    assert result["removed"] == 0
    assert result["kept"] == 0
    assert (worktrees_dir / "notes.txt").is_file()


def test_worktree_add_failure_cleans_orphan_branch(tmp_path, monkeypatch):
    """A failed add removes only the branch this invocation reserved (#398)."""
    from drover.server.harness.worktree import WorktreeIsolationUnavailable

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    real_run = subprocess.run

    def create_branch_then_stall(cmd, *args, **kwargs):
        if "worktree" in cmd and "add" in cmd:
            # The branch is reserved before a potentially slow worktree add.
            reserved = real_run(
                ["git", "-C", str(repo), "branch", "--list", "drover/harness-orphan"],
                capture_output=True,
                text=True,
                check=True,
            )
            assert reserved.stdout.strip()
            raise subprocess.TimeoutExpired(cmd, 90)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", create_branch_then_stall)

    with pytest.raises(WorktreeIsolationUnavailable):
        create_session_worktree(str(repo), "harness-orphan", tmp_path / "worktrees")

    assert _git(repo, "branch", "--list", "drover/harness-orphan") == ""


def test_repeated_session_id_preserves_existing_dirty_worktree(repo, tmp_path):
    """A failed add cannot claim ownership of an earlier session's work."""
    from drover.server.harness.worktree import WorktreeIsolationUnavailable

    worktrees_dir = tmp_path / "worktrees"
    wt = create_session_worktree(str(repo), "harness-existing", worktrees_dir)
    assert wt is not None
    path = worktrees_dir / "harness-existing"
    (path / "wip.txt").write_text("only copy\n")

    with pytest.raises(WorktreeIsolationUnavailable):
        create_session_worktree(str(repo), "harness-existing", worktrees_dir)

    assert (path / "wip.txt").read_text() == "only copy\n"
    assert _git(repo, "branch", "--list", wt.branch) != ""


def test_failed_add_does_not_delete_an_existing_orphan_branch(repo, tmp_path):
    """A branch present before the add belongs to an earlier attempt."""
    from drover.server.harness.worktree import WorktreeIsolationUnavailable

    branch = "drover/harness-existing-orphan"
    _git(repo, "branch", branch)
    before = _git(repo, "rev-parse", branch)

    with pytest.raises(WorktreeIsolationUnavailable):
        create_session_worktree(str(repo), "harness-existing-orphan", tmp_path / "wt")

    assert _git(repo, "rev-parse", branch) == before


def test_a_directory_that_cannot_host_a_worktree_still_returns_none(tmp_path):
    """The legitimate fallback must survive: no repo means run in place."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert create_session_worktree(str(plain), "harness-x", tmp_path / "wt") is None


def test_worktree_add_gets_a_launch_sized_timeout(tmp_path, monkeypatch):
    """A probe may fail fast; creating the worktree must not.

    On the reference hub, process start on the external SSD spikes from 0.05s to
    over 11s (drover#321). With the same 15s budget as a read-only probe,
    `git worktree add` timed out twice in two minutes and, correctly failing
    closed, refused two codex launches that would have succeeded. The hub's own
    create budget is 120s, so the add fits well inside it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    seen = {}
    real_run = subprocess.run

    def record(cmd, *args, **kwargs):
        if "worktree" in cmd and "add" in cmd:
            seen["timeout"] = kwargs.get("timeout")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", record)
    assert create_session_worktree(str(repo), "harness-t", tmp_path / "wt") is not None
    assert seen["timeout"] is not None and 60 <= seen["timeout"] < 120
