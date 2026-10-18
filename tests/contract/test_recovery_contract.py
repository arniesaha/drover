"""Restart and recovery routes exercised through durable daemon state."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from .support import wait_until
from .test_harnessd_contract import request


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


def test_recovery_rejects_terminated_and_terminal_sessions(daemon):
    _, structured = request(
        daemon,
        "/sessions",
        payload={
            "harness": "claude-code",
            "mode": "structured",
            "cwd": str(daemon.root),
        },
    )
    assert (
        request(daemon, f"/sessions/{structured['session_id']}/terminate", payload={})[
            0
        ]
        == 200
    )
    status, terminated = request(
        daemon, f"/sessions/{structured['session_id']}/recover", payload={}
    )
    assert (status, terminated["error"]) == (
        409,
        "Session cannot be resumed after the harness restart. Continue it in a new session.",
    )

    _, terminal = request(
        daemon, "/sessions", payload={"harness": "shell", "cwd": str(daemon.root)}
    )
    status, unsupported = request(
        daemon, f"/sessions/{terminal['session_id']}/recover", payload={}
    )
    assert (status, unsupported["error"]) == (
        409,
        "Session cannot be resumed after the harness restart. Continue it in a new session.",
    )


def test_concurrent_recovery_creates_one_driver(daemon):
    _, created = request(
        daemon,
        "/sessions",
        payload={
            "harness": "claude-code",
            "mode": "structured",
            "cwd": str(daemon.root),
        },
    )
    session_id = created["session_id"]
    daemon.restart()

    def recover():
        return request(
            daemon,
            f"/sessions/{session_id}/recover",
            payload={"native_session_id": "claude-native-1"},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _unused: recover(), range(4)))
    assert all(status == 200 for status, _body in results)
    assert sum(body["recovered"] for _status, body in results) == 1


def test_orphaned_pty_session_is_reconciled_on_restart(daemon):
    _, created = request(
        daemon, "/sessions", payload={"harness": "shell", "cwd": str(daemon.root)}
    )
    daemon.restart()
    status, session = request(daemon, f"/sessions/{created['session_id']}")
    assert status == 200
    assert session["status"] in {"completed", "errored", "terminated"}


def test_harnessd_terminate_waits_for_recovery_and_wins(daemon):
    _, created = request(
        daemon,
        "/sessions",
        payload={
            "harness": "claude-code",
            "mode": "structured",
            "cwd": str(daemon.root),
        },
    )
    session_id = created["session_id"]
    daemon.restart()
    barrier = Barrier(2)

    def recover():
        barrier.wait()
        return request(
            daemon,
            f"/sessions/{session_id}/recover",
            payload={"native_session_id": "claude-native-1"},
        )

    def terminate():
        barrier.wait()
        return request(daemon, f"/sessions/{session_id}/terminate", payload={})

    with ThreadPoolExecutor(max_workers=2) as pool:
        recovery, termination = list(
            pool.map(lambda operation: operation(), (recover, terminate))
        )
    assert recovery[0] in {200, 409}
    assert termination[0] == 200
    assert request(daemon, f"/sessions/{session_id}")[1]["status"] == "terminated"
