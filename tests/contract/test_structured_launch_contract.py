"""Structured-launch contracts visible from the daemon HTTP API."""

from __future__ import annotations

import json
import subprocess
import sys

from .support import wait_until
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


def test_native_resume_launches_through_the_structured_adapter(daemon):
    status, created = request(
        daemon,
        "/sessions",
        payload={
            "harness": "claude-code",
            "mode": "structured",
            "cwd": str(daemon.root),
            "native_resume": {"session_id": "claude-native-1", "label": "work"},
        },
    )
    assert status == 201
    assert created["mode"] == "structured"
    log = daemon.root / "fake-cli.jsonl"
    wait_until(lambda: log.exists() and "--resume" in log.read_text())
    launches = [
        row["argv"]
        for line in log.read_text().splitlines()
        if "argv" in (row := json.loads(line))
    ]
    assert any(argv[-2:] == ["--resume", "claude-native-1"] for argv in launches)
    for native_resume in ({"latest": True}, {"mode": "continue"}):
        status, body = request(
            daemon,
            "/sessions",
            payload={
                "harness": "claude-code",
                "mode": "structured",
                "cwd": str(daemon.root),
                "native_resume": native_resume,
            },
        )
        assert (status, body["error"]) == (400, "native_resume requires a session_id")


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


def test_initial_structured_executable_error_closes_entry_and_cleans_worktree(daemon):
    repo = daemon.root / "failing-repo"
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
        payload={
            "harness": "codex",
            "mode": "structured",
            "cwd": str(repo),
            "command": ["/definitely/not-a-command"],
        },
    )
    assert status == 201
    worktree = daemon.root / "worktrees" / created["session_id"]
    status, body = request(
        daemon, f"/sessions/{created['session_id']}/turns", payload={"text": "start"}
    )
    assert (status, body["error"].startswith("structured harness launch failed:")) == (
        502,
        True,
    )
    assert not worktree.exists()
