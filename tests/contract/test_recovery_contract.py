"""Restart and recovery routes exercised through durable daemon state."""

from __future__ import annotations

from .test_harnessd_contract import request
from .support import wait_until


def test_structured_session_can_recover_after_daemon_restart(daemon):
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
    session_id = created["session_id"]
    daemon.restart()
    status, session = request(daemon, f"/sessions/{session_id}")
    assert status == 200
    assert session["status"] == "errored"
    status, rejected = request(daemon, f"/sessions/{session_id}/recover", payload={})
    assert (status, rejected["error"]) == (
        409,
        "Session cannot be resumed after the harness restart. Continue it in a new session.",
    )
    status, recovered = request(
        daemon,
        f"/sessions/{session_id}/recover",
        payload={"native_session_id": "claude-native-1"},
    )
    assert status == 200, recovered
    assert recovered["recovered"] is True
    log = daemon.root / "fake-cli.jsonl"
    wait_until(lambda: log.exists() and "--resume" in log.read_text())
    status, repeated = request(
        daemon,
        f"/sessions/{session_id}/recover",
        payload={"native_session_id": "claude-native-1"},
    )
    assert (status, repeated["recovered"], repeated["status"]) == (
        200,
        False,
        "running",
    )


def test_restart_lost_session_can_be_terminated(daemon):
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
    session_id = created["session_id"]
    daemon.restart()
    status, terminated = request(
        daemon, f"/sessions/{session_id}/terminate", payload={}
    )
    assert (
        status,
        terminated["session_id"],
        terminated["status"],
        terminated["terminated"],
    ) == (
        200,
        session_id,
        "terminated",
        True,
    )
    assert request(daemon, f"/sessions/{session_id}")[1]["status"] == "terminated"


def test_live_structured_termination_persists_in_inventory(daemon):
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
    session_id = created["session_id"]
    assert request(daemon, f"/sessions/{session_id}/terminate", payload={})[0] == 200
    assert request(daemon, f"/sessions/{session_id}")[1]["status"] == "terminated"
    sessions = request(daemon, "/sessions")[1]["sessions"]
    assert [(item["session_id"], item["status"]) for item in sessions] == [
        (session_id, "terminated")
    ]
