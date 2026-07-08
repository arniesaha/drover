"""Tests for drover.hook.context — agent_id + git resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from drover.hook.context import AgentContext, detect_context


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_detect_in_real_git_repo(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", "git@github.com:owner/myrepo.git")
    (repo / "README.md").write_text("hi")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")

    ctx = detect_context(cwd=repo, hook_config={"agent_id": "test-agent"})
    assert ctx.agent_id == "test-agent"
    assert ctx.repo_owner == "owner"
    assert ctx.repo_name == "myrepo"
    assert ctx.branch == "main"


def test_detect_outside_git_returns_none_repo_fields(tmp_path: Path) -> None:
    ctx = detect_context(cwd=tmp_path, hook_config={"agent_id": "isolated-agent"})
    assert ctx.agent_id == "isolated-agent"
    assert ctx.repo_owner is None
    assert ctx.repo_name is None
    assert ctx.branch is None


def test_detect_default_agent_id_uses_hostname_claude(tmp_path: Path) -> None:
    ctx = detect_context(cwd=tmp_path, hook_config=None)
    assert ctx.agent_id.endswith("-claude")
    assert len(ctx.agent_id) > len("-claude")


def test_detect_https_remote_url(tmp_path: Path) -> None:
    repo = tmp_path / "https-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "remote", "add", "origin", "https://github.com/foo/bar.git")
    (repo / "x").write_text("x")
    _git(repo, "add", "x")
    _git(repo, "commit", "-q", "-m", "i")

    ctx = detect_context(cwd=repo, hook_config={"agent_id": "x"})
    assert (ctx.repo_owner, ctx.repo_name) == ("foo", "bar")


def test_detect_branch_change(tmp_path: Path) -> None:
    repo = tmp_path / "branchy"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "x").write_text("x")
    _git(repo, "add", "x")
    _git(repo, "commit", "-q", "-m", "i")
    _git(repo, "checkout", "-q", "-b", "feature/foo")

    ctx = detect_context(cwd=repo, hook_config={"agent_id": "x"})
    assert ctx.branch == "feature/foo"
