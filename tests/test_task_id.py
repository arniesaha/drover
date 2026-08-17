"""Tests for src/drover/task_id.py."""

import os

from drover.task_id import compute_task_id, parse_repo_url


def test_branch_default_derivation():
    a = compute_task_id(
        env_task_id=None, repo_owner="arniesaha", repo_name="nexus", branch="main"
    )
    b = compute_task_id(
        env_task_id=None, repo_owner="arniesaha", repo_name="nexus", branch="main"
    )
    assert a == b
    assert len(a) == 16


def test_env_override_wins():
    a = compute_task_id(
        env_task_id="my-task-001", repo_owner="x", repo_name="y", branch="z"
    )
    b = compute_task_id(
        env_task_id="my-task-001", repo_owner="diff", repo_name="diff", branch="diff"
    )
    assert a == b


def test_different_branches_produce_different_ids():
    a = compute_task_id(None, "arniesaha", "nexus", "main")
    b = compute_task_id(None, "arniesaha", "nexus", "feature/foo")
    assert a != b


def test_branch_none_falls_back_to_HEAD():
    # Non-git context: still produces a stable id (uses literal "HEAD")
    a = compute_task_id(None, "arniesaha", "nexus", None)
    b = compute_task_id(None, "arniesaha", "nexus", None)
    assert a == b


def test_parse_repo_url_ssh():
    owner, name = parse_repo_url("git@github.com:arniesaha/nexus.git")
    assert owner == "arniesaha"
    assert name == "nexus"


def test_parse_repo_url_https():
    owner, name = parse_repo_url("https://github.com/arniesaha/nexus.git")
    assert owner == "arniesaha"
    assert name == "nexus"


def test_parse_repo_url_no_dot_git():
    owner, name = parse_repo_url("https://github.com/arniesaha/nexus")
    assert owner == "arniesaha"
    assert name == "nexus"


def test_parse_repo_url_returns_none_none_on_garbage():
    owner, name = parse_repo_url("not-a-url")
    assert owner is None
    assert name is None
