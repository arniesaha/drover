"""Public HTTP and command-line contracts for a launched harnessd."""

from __future__ import annotations

import subprocess
import sys

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


def test_model_catalog_route_is_host_scoped_and_forceable(daemon):
    status, body = request(daemon, "/model-catalog?harness=codex&refresh=1")
    assert status == 200
    assert body["host_id"] == "contract-host"
    assert body["harness"] == "codex"
    assert isinstance(body["models"], list)


def test_model_catalog_route_returns_safe_degraded_envelope(daemon):
    status, body = request(daemon, "/model-catalog?harness=codex")
    assert status == 200
    assert body["host_id"] == "contract-host"
    assert body["harness"] == "codex"
    assert {"models", "stale", "stale_reason"} <= body.keys()
    assert isinstance(body["models"], list)


def test_model_preference_validation_rejects_launch_before_side_effects(daemon):
    status, body = _structured(daemon, harness="codex", model="definitely-not-a-model")
    assert status == 400
    assert body == {
        "error": "This model is no longer available; refresh model choices and try again."
    }
    assert request(daemon, "/sessions")[1]["sessions"] == []


def test_model_preference_validation_keeps_null_launch_defaults(daemon):
    status, body = _structured(daemon)
    assert status == 201
    assert (body["model"], body["thinking_effort"]) == (None, None)


def test_model_preference_validation_preserves_opaque_identifier_whitespace(daemon):
    status, body = _structured(daemon, harness="codex", model=" contract-model ")
    assert status == 400
    assert body == {
        "error": "This model is no longer available; refresh model choices and try again."
    }


def test_model_preference_validation_rejects_turn_before_attachment_or_driver(daemon):
    status, created = _structured(daemon, harness="codex")
    assert status == 201
    status, body = request(
        daemon,
        f"/sessions/{created['session_id']}/turns",
        payload={"text": "continue", "model": "definitely-not-a-model"},
    )
    assert status == 400
    assert body == {
        "error": "This model is no longer available; refresh model choices and try again."
    }


def test_client_turn_id_requires_a_uuid(daemon):
    status, created = _structured(daemon)
    assert status == 201
    status, body = request(
        daemon,
        f"/sessions/{created['session_id']}/turns",
        payload={"text": "continue", "client_turn_id": "not-a-uuid"},
    )
    assert (status, body) == (400, {"error": "client_turn_id must be a UUID"})


def test_model_preference_validation_discards_claude_turn_overrides(daemon):
    status, created = _structured(daemon)
    assert status == 201
    status, body = request(
        daemon,
        f"/sessions/{created['session_id']}/turns",
        payload={
            "text": "continue",
            "model": "definitely-not-a-model",
            "thinking_effort": "impossible",
        },
    )
    assert status == 202
    assert isinstance(body["turn_id"], str)


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "-m", "drover.server", "harnessd", "--help"],
        [sys.executable, "-m", "drover.server.harness.cli", "--help"],
    ],
)
def test_harnessd_cli_help_documents_core_options(command):
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    for option in ("--host-id", "--listen", "--local-url", "--tailscale-url"):
        assert option in completed.stdout


def test_skinny_harnessd_entrypoint_documents_core_options():
    completed = subprocess.run(
        [sys.executable, "-m", "drover.server.harness.cli", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    for option in ("--central-url", "--host-token", "--relay"):
        assert option in completed.stdout


def test_harnessd_health_and_capabilities(daemon):
    assert request(daemon, "/healthz", token=False) == (
        200,
        {"ok": True, "host_id": "contract-host", "active_sessions": 0},
    )
    status, body = request(daemon, "/capabilities")
    assert status == 200
    harnesses = {harness["name"]: harness for harness in body["harnesses"]}
    assert harnesses["shell"]["enabled"] is True
    assert harnesses["codex"]["enabled"] is True


def test_every_offered_preset_can_actually_be_driven(daemon):
    status, capabilities = request(daemon, "/capabilities")
    assert status == 200
    offered = {item["name"] for item in capabilities["harnesses"] if item["enabled"]}
    assert {"shell", "claude-code", "codex", "agy"} <= offered

    for harness in ("claude-code", "codex", "agy"):
        status, created = request(
            daemon,
            "/sessions",
            payload={"harness": harness, "mode": "structured", "cwd": str(daemon.root)},
        )
        assert status == 201
        assert (
            request(daemon, f"/sessions/{created['session_id']}/terminate", payload={})[
                0
            ]
            == 200
        )

    status, shell = request(
        daemon, "/sessions", payload={"harness": "shell", "cwd": str(daemon.root)}
    )
    assert status == 201
    assert (
        request(daemon, f"/sessions/{shell['session_id']}/terminate", payload={})[0]
        == 200
    )
