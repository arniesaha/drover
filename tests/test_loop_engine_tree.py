"""What the loop can say about the tree an iteration left behind."""

from __future__ import annotations

import subprocess
from pathlib import Path

from loop_engine.tree import head_commit, is_reachable


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "loop@example.invalid")
    _git(repo, "config", "user.name", "Loop")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


def test_head_commit_reads_the_checkout(tmp_path):
    repo = _repo(tmp_path)
    assert head_commit(repo) == _git(repo, "rev-parse", "HEAD")


def test_head_commit_is_none_outside_a_checkout(tmp_path):
    """A driver that cannot read git still runs the iteration."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert head_commit(plain) is None


def test_a_commit_still_in_the_tree_is_reachable(tmp_path):
    repo = _repo(tmp_path)
    first = head_commit(repo)
    (repo / "b.txt").write_text("two\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "second")

    assert is_reachable(repo, first)
    assert is_reachable(repo, head_commit(repo)), "HEAD is reachable from itself"


def test_a_commit_a_reset_threw_away_is_not_reachable(tmp_path):
    """The drover#280-shaped mistake, in git: the claim outlives the artifact."""
    repo = _repo(tmp_path)
    base = head_commit(repo)
    (repo / "b.txt").write_text("two\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "second")
    orphaned = head_commit(repo)
    _git(repo, "reset", "-q", "--hard", base)

    assert not is_reachable(repo, orphaned)
    assert is_reachable(repo, base)


def test_an_empty_ref_is_never_reachable(tmp_path):
    assert not is_reachable(_repo(tmp_path), "")
