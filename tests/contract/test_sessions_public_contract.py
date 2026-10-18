"""Public-process ports of session inventory and idempotency contracts."""

from __future__ import annotations

from .test_harnessd_contract import request


def _shell(daemon, **extra):
    return request(
        daemon,
        "/sessions",
        payload={"harness": "shell", "cwd": str(daemon.root), **extra},
    )


def test_shell_create_get_list_and_terminate(daemon):
    status, created = _shell(daemon)
    assert status == 201
    assert created["status"] == "running"
    assert isinstance(created["pid"], int) and created["pid"] > 0
    sid = created["session_id"]
    status, listed = request(daemon, "/sessions")
    assert status == 200
    assert [row["session_id"] for row in listed["sessions"]] == [sid]
    status, fetched = request(daemon, f"/sessions/{sid}")
    assert status == 200
    assert fetched["pid"] == created["pid"]
    status, terminated = request(daemon, f"/sessions/{sid}/terminate", payload={})
    assert status == 200
    assert terminated == {
        "host_id": "contract-host",
        "session_id": sid,
        "status": "terminated",
        "terminated": True,
    }


def test_client_session_key_is_idempotent(daemon):
    payload = {
        "client_session_id": "contract-key",
        "command": ["/bin/sh", "-c", "sleep 20"],
    }
    first_status, first = _shell(daemon, **payload)
    second_status, second = _shell(daemon, **payload)
    assert first_status == 201
    assert second_status == 200
    assert second["session_id"] == first["session_id"]
    assert second["deduplicated"] is True
    assert len(request(daemon, "/sessions")[1]["sessions"]) == 1


def test_unkeyed_creates_are_distinct(daemon):
    first_status, first = _shell(daemon, command=["/bin/sh", "-c", "sleep 20"])
    second_status, second = _shell(daemon, command=["/bin/sh", "-c", "sleep 20"])
    assert first_status == second_status == 201
    assert first["session_id"] != second["session_id"]


def test_handoff_source_is_idempotent_but_parent_is_not(daemon):
    base = {"harness": "claude-code", "mode": "structured", "cwd": str(daemon.root)}
    first_status, first = request(
        daemon,
        "/sessions",
        payload={
            **base,
            "source_session_id": "source-a",
            "handoff_mode": "nexus_handoff",
        },
    )
    second_status, second = request(
        daemon,
        "/sessions",
        payload={
            **base,
            "source_session_id": "source-a",
            "handoff_mode": "nexus_handoff",
        },
    )
    assert first_status == 201 and second_status == 200
    assert first["session_id"] == second["session_id"]
    one_status, one = request(
        daemon, "/sessions", payload={**base, "parent_session_id": "parent-a"}
    )
    two_status, two = request(
        daemon, "/sessions", payload={**base, "parent_session_id": "parent-a"}
    )
    assert one_status == two_status == 201
    assert one["session_id"] != two["session_id"]
