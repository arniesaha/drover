"""Session mutations observed through the public HTTP inventory."""

from __future__ import annotations

import pytest

from .test_harnessd_contract import request


def _structured(daemon, **extra):
    return request(
        daemon,
        "/sessions",
        payload={
            "harness": "claude-code",
            "mode": "structured",
            "cwd": str(daemon.root),
            **extra,
        },
    )


@pytest.mark.parametrize(
    "operation",
    [
        "shell-create",
        "shell-list-get",
        "shell-delete",
        "structured-create",
        "structured-terminate",
        "structured-default-permission",
        "structured-explicit-permission",
        "client-key-idempotency",
        "unkeyed-creates",
        "handoff-source-idempotency",
        "different-handoff-sources",
        "delegated-children",
    ],
)
def test_public_session_mutation_contract(daemon, operation):
    if operation.startswith("shell"):
        status, created = request(
            daemon, "/sessions", payload={"harness": "shell", "cwd": str(daemon.root)}
        )
        assert status == 201
        if operation == "shell-create":
            assert created["status"] == "running" and created["pid"] > 0
        elif operation == "shell-list-get":
            assert created["session_id"] in {
                item["session_id"]
                for item in request(daemon, "/sessions")[1]["sessions"]
            }
            assert (
                request(daemon, f"/sessions/{created['session_id']}")[1]["pid"]
                == created["pid"]
            )
        else:
            status, deleted = request(
                daemon, f"/sessions/{created['session_id']}", method="DELETE"
            )
            assert (status, deleted["status"]) == (200, "terminated")
        return

    if operation.startswith("structured"):
        extra = (
            {"permission_mode": "auto"}
            if operation.endswith("explicit-permission")
            else {}
        )
        status, created = _structured(daemon, **extra)
        assert status == 201
        if operation == "structured-terminate":
            status, body = request(
                daemon, f"/sessions/{created['session_id']}/terminate", payload={}
            )
            assert (status, body["terminated"]) == (200, True)
        else:
            session = request(daemon, f"/sessions/{created['session_id']}")[1]
            assert session["mode"] == "structured"
        return

    if operation == "client-key-idempotency":
        payload = {
            "harness": "claude-code",
            "mode": "structured",
            "cwd": str(daemon.root),
            "client_session_id": "client-1",
        }
        first = request(daemon, "/sessions", payload=payload)[1]
        second = request(daemon, "/sessions", payload=payload)[1]
        assert first["session_id"] == second["session_id"]
        return

    if operation == "unkeyed-creates":
        first = _structured(daemon)[1]
        second = _structured(daemon)[1]
        assert first["session_id"] != second["session_id"]
        return

    payload = {
        "harness": "claude-code",
        "mode": "structured",
        "cwd": str(daemon.root),
    }
    if operation != "delegated-children":
        payload.update(
            {"source_session_id": "source-a", "handoff_mode": "nexus_handoff"}
        )
    else:
        payload["parent_session_id"] = "parent"
    first = request(daemon, "/sessions", payload=payload)[1]
    if operation == "different-handoff-sources":
        payload["source_session_id"] = "source-b"
    elif operation == "delegated-children":
        pass
    second = request(daemon, "/sessions", payload=payload)[1]
    expected_same = operation == "handoff-source-idempotency"
    assert (first["session_id"] == second["session_id"]) is expected_same
