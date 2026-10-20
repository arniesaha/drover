"""Startup contracts exercised through launcher options and the fake hub."""

from __future__ import annotations

import pytest

from .support import wait_until
from .test_harnessd_contract import request


@pytest.mark.parametrize(
    "daemon",
    [{"hub_responses": [("/harness/hosts", 200, {"relay_required": True})]}],
    indirect=True,
)
def test_bound_host_in_direct_mode_refuses_startup_with_relay_guidance(
    daemon, fake_hub
):
    daemon.process.wait(timeout=5)
    assert daemon.process.returncode == 0, daemon.process.stderr.read()
    wait_until(
        lambda: any(item["path"] == "/harness/hosts" for item in fake_hub.requests)
    )


@pytest.mark.parametrize(
    "daemon",
    [
        {
            "advisory_target_text": "content\n",
            "preseed_consent": {"enabled": True, "epoch": 4},
            "hub_responses": [
                (
                    "/harness/hosts",
                    200,
                    {"content_consent": {"enabled": False, "epoch": 5}},
                )
            ],
        }
    ],
    indirect=True,
)
def test_reconnecting_daemon_reconciles_revoked_consent_from_registration(daemon):
    status, body = request(
        daemon, "/advisory/content-bundle", payload={"target_ids": ["AGENTS.md"]}
    )
    assert (status, body) == (403, {"error": "content analysis is disabled"})


@pytest.mark.parametrize("daemon", [{"no_central": True}], indirect=True)
def test_wire_event_pusher_is_noop_without_central_url_or_token(daemon, fake_hub):
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
    assert not [item for item in fake_hub.requests if item["path"] == "/harness/events"]
