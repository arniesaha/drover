"""Structured-launch contracts visible from the daemon HTTP API."""

from __future__ import annotations

import subprocess
import sys

from .test_harnessd_contract import request


def test_structured_launch_cannot_bypass_a_missing_adapter(daemon):
    status, body = request(
        daemon,
        "/sessions",
        payload={"harness": "shell", "mode": "structured", "cwd": str(daemon.root)},
    )
    assert status == 400
    assert body == {"error": "harness has no structured driver: shell"}


def test_structured_codex_session_non_git_cwd_runs_in_place(daemon):
    status, created = request(
        daemon,
        "/sessions",
        payload={"harness": "codex", "mode": "structured", "cwd": str(daemon.root)},
    )
    assert status == 201
    assert created["cwd"] == str(daemon.root)


def test_structured_codex_session_runs_in_worktree(daemon):
    repo = daemon.root / "repo"
    repo.mkdir()
    (repo / "file.txt").write_text("base\n")
    for command in (
        ["git", "init", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "contract@example.test"],
        ["git", "-C", str(repo), "config", "user.name", "Contract"],
        ["git", "-C", str(repo), "add", "file.txt"],
        ["git", "-C", str(repo), "commit", "-m", "initial"],
    ):
        subprocess.run(command, check=True, capture_output=True)

    status, created = request(
        daemon,
        "/sessions",
        payload={"harness": "codex", "mode": "structured", "cwd": str(repo)},
    )
    assert status == 201
    session_id = created["session_id"]
    worktree = daemon.root / "worktrees" / session_id
    assert created["cwd"] == str(worktree)
    assert (worktree / "file.txt").read_text() == "base\n"

    assert request(daemon, f"/sessions/{session_id}/terminate", payload={})[0] == 200
    assert not worktree.exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", f"drover/{session_id}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert not branches.strip()


def test_structured_claude_session_stays_in_place(daemon):
    status, created = request(
        daemon,
        "/sessions",
        payload={
            "harness": "claude-code",
            "mode": "structured",
            "cwd": str(daemon.root),
        },
    )
    assert status == 201
    assert created["cwd"] == str(daemon.root)


def test_missing_structured_executable_turn_returns_json_and_marks_session_errored(
    daemon,
):
    status, created = request(
        daemon,
        "/sessions",
        payload={
            "harness": "codex",
            "mode": "structured",
            "cwd": str(daemon.root),
            "command": [sys.executable, "-c", "raise SystemExit(0)"],
        },
    )
    assert status == 201
    status, body = request(
        daemon, f"/sessions/{created['session_id']}/turns", payload={"text": "continue"}
    )
    assert status in {202, 502}
    if status == 502:
        assert body["error"].startswith("structured harness launch failed:")
