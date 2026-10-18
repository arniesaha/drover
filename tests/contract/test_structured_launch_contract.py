"""Structured-launch contracts visible from the daemon HTTP API."""

from __future__ import annotations

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
