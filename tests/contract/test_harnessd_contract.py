"""HTTP contracts shared by the Python daemon and its future Rust port."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from .conftest import wait_until


def request(daemon, path, *, payload=None, token=True, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {daemon.token}"
    request = urllib.request.Request(
        daemon.url + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_launcher_announces_bound_port_and_registers(daemon, fake_hub):
    status, health = request(daemon, "/healthz", token=False)
    assert status == 200
    assert health == {"ok": True, "host_id": "contract-host", "active_sessions": 0}
    wait_until(lambda: any(r["path"] == "/harness/hosts" for r in fake_hub.requests))
    registration = next(r for r in fake_hub.requests if r["path"] == "/harness/hosts")
    assert registration["authorization"] == "Bearer contract-token"
    assert registration["body"]["host_id"] == "contract-host"
    assert registration["body"]["connection_kind"] == "direct"


def test_auth_and_top_level_error_envelopes(daemon):
    status, body = request(daemon, "/sessions", token=False)
    assert (status, body) == (401, {"error": "authentication required"})
    assert request(daemon, "/not-a-route") == (404, {"error": "not found"})
    status, capabilities = request(daemon, "/capabilities")
    assert status == 200
    assert {"host_id", "harnesses"} <= capabilities.keys()
    assert capabilities["host_id"] == "contract-host"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/sessions/nope", "unknown terminal session: nope"),
        ("/sessions/nope/turns", "unknown structured session: nope"),
        ("/sessions/nope/interrupt", "unknown structured session: nope"),
        ("/sessions/nope/permission", "unknown structured session: nope"),
    ],
)
def test_unknown_session_errors_are_wire_stable(daemon, path, expected):
    method = (
        "POST"
        if path.rsplit("/", 1)[-1] in {"turns", "interrupt", "permission"}
        else None
    )
    status, body = request(daemon, path, payload={} if method else None, method=method)
    assert status == 404
    assert body == {"error": expected}


def test_inventory_cursor_and_cors_contract(daemon):
    status, inventory = request(daemon, "/sessions")
    assert status == 200
    assert set(inventory) == {
        "host_id",
        "sessions",
        "archived_limit",
        "next_archived_cursor",
    }
    assert inventory["sessions"] == []
    status, body = request(daemon, "/sessions?archived_cursor=bad")
    assert (status, body) == (400, {"error": "invalid archived_cursor"})


def test_structured_lifecycle_and_event_push_contract(daemon, fake_hub):
    status, created = request(
        daemon,
        "/sessions",
        payload={
            "harness": "claude-code",
            "mode": "structured",
            "cwd": str(daemon.root),
            "prompt": "hello",
        },
    )
    assert status == 201
    assert created["mode"] == "structured"
    sid = created["session_id"]

    wait_until(
        lambda: request(daemon, f"/sessions/{sid}")[1].get("awaiting") == "input"
    )
    status, turn = request(daemon, f"/sessions/{sid}/turns", payload={"text": "next"})
    assert status == 202 and isinstance(turn["turn_id"], str)
    wait_until(lambda: any(r["path"] == "/harness/events" for r in fake_hub.requests))
    pushed = [
        event
        for r in fake_hub.requests
        if r["path"] == "/harness/events"
        for event in r["body"]["events"]
    ]
    assert pushed
    assert all(isinstance(event["seq"], int) for event in pushed)
    assert [event["seq"] for event in pushed] == sorted(
        event["seq"] for event in pushed
    )
    assert all(event["event_id"] for event in pushed)
    assert (
        request(daemon, f"/sessions/{sid}/terminate", payload={})[1]["terminated"]
        is True
    )


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"harness": "shell", "mode": "structured"}, "shell"),
        (
            {"harness": "claude-code", "mode": "structured", "permission_mode": "ask"},
            "not supported yet",
        ),
        (
            {
                "harness": "claude-code",
                "mode": "structured",
                "permission_mode": "bogus",
            },
            "unknown permission_mode",
        ),
        ({"harness": "unknown", "mode": "structured"}, "unknown"),
    ],
)
def test_structured_launch_validation_contract(daemon, payload, fragment):
    status, body = request(daemon, "/sessions", payload=payload)
    assert status == 400
    assert fragment in body["error"]
