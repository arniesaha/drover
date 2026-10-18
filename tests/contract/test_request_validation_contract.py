"""Wire-level refusal contracts for public harnessd requests."""

from __future__ import annotations

import json
import urllib.request

import pytest

from .test_harnessd_contract import request


@pytest.mark.parametrize(
    ("path", "payload", "status", "fragment"),
    [
        ("/auth/codex/status", None, 200, "harness"),
        ("/auth/codex%2F/status", None, 404, "unknown"),
        ("/auth/codex/flows/no-such-flow", None, 404, "unknown"),
        ("/advisory/content-bundle", {"target_ids": ["AGENTS.md"]}, 403, "disabled"),
        ("/sessions", {"harness": "unknown", "mode": "structured"}, 400, "unknown"),
        ("/sessions", {"harness": "codex", "mode": "pty"}, 400, "pty"),
        ("/sessions", {"harness": "shell", "mode": "structured"}, 400, "shell"),
        (
            "/sessions",
            {"harness": "claude-code", "mode": "structured", "permission_mode": "ask"},
            400,
            "not supported",
        ),
        (
            "/sessions",
            {
                "harness": "claude-code",
                "mode": "structured",
                "permission_mode": "not-a-mode",
            },
            400,
            "unknown permission_mode",
        ),
        (
            "/sessions/no-such-session/turns",
            {"text": "hello"},
            404,
            "unknown structured session",
        ),
        ("/sessions/no-such-session/interrupt", {}, 404, "unknown structured session"),
        ("/sessions/no-such-session/permission", {}, 404, "unknown structured session"),
    ],
)
def test_public_route_validation(daemon, path, payload, status, fragment):
    method = "POST" if payload is not None else None
    observed_status, body = request(daemon, path, payload=payload, method=method)
    assert observed_status == status
    assert fragment in body["error"] if status >= 400 else fragment in body


@pytest.mark.parametrize(
    "payload, fragment",
    [
        (
            {
                "harness": "claude-code",
                "mode": "structured",
                "images": [{"media_type": "image/png", "data_base64": "%%"}],
            },
            "base64",
        ),
        (
            {
                "harness": "claude-code",
                "mode": "structured",
                "images": [
                    {"media_type": "application/pdf", "data_base64": "aGVsbG8="}
                ],
            },
            "attachment type",
        ),
    ],
)
def test_structured_launch_rejects_invalid_turn_input(daemon, payload, fragment):
    status, body = request(daemon, "/sessions", payload=payload)
    assert status == 400
    assert fragment in body["error"]


def test_empty_turn_still_rejected(daemon):
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
    status, body = request(
        daemon, f"/sessions/{created['session_id']}/turns", payload={}
    )
    assert (status, body) == (400, {"error": "text or images required"})


def test_harnessd_sessions_cors_preflight_and_post_headers(daemon):
    preflight = urllib.request.Request(daemon.url + "/sessions", method="OPTIONS")
    with urllib.request.urlopen(preflight, timeout=5) as response:
        assert response.status == 204
        assert response.headers["Access-Control-Allow-Origin"] == "*"
        assert "POST" in response.headers["Access-Control-Allow-Methods"]

    data = json.dumps({"harness": "shell", "cwd": str(daemon.root)}).encode()
    post = urllib.request.Request(
        daemon.url + "/sessions",
        data=data,
        headers={
            "Authorization": f"Bearer {daemon.token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(post, timeout=5) as response:
        assert response.status == 201
        assert response.headers["Access-Control-Allow-Origin"] == "*"


def test_harnessd_content_bundle_requires_auth(daemon):
    status, body = request(
        daemon,
        "/advisory/content-bundle",
        payload={"target_ids": ["AGENTS.md"]},
        token=False,
    )
    assert (status, body) == (401, {"error": "authentication required"})
