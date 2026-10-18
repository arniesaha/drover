"""Managed device-login flow against a configurable CLI-shaped executable."""

from __future__ import annotations

from .support import wait_until
from .test_harnessd_contract import request


def test_harnessd_auth_start_poll_and_cancel(daemon):
    status, started = request(daemon, "/auth/codex/start", payload={})
    assert status == 200
    assert (started["host_id"], started["harness"], started["supports_input"]) == (
        "contract-host",
        "codex",
        False,
    )
    flow_path = f"/auth/codex/flows/{started['flow_id']}"
    wait_until(lambda: request(daemon, flow_path)[1].get("login_url") is not None)
    status, polled = request(daemon, flow_path)
    assert (status, polled["login_url"], polled["user_code"]) == (
        200,
        "https://example.test/device",
        "ABCD-EFGH",
    )
    status, cancelled = request(daemon, f"{flow_path}/cancel", payload={})
    assert (status, cancelled["state"], cancelled["host_id"]) == (
        200,
        "cancelled",
        "contract-host",
    )


def test_harnessd_routes_typed_code_into_a_pty_login_flow(daemon):
    status, started = request(daemon, "/auth/claude-code/start", payload={})
    assert status == 200
    flow_path = f"/auth/claude-code/flows/{started['flow_id']}"
    wait_until(lambda: request(daemon, flow_path)[1].get("supports_input") is True)
    assert (
        request(daemon, f"{flow_path}/input", payload={"text": "PASTED-CODE"})[0] == 200
    )
    wait_until(lambda: request(daemon, flow_path)[1].get("state") == "authenticated")
    assert request(daemon, "/auth/claude-code/status")[1]["state"] == "authenticated"


def test_harnessd_refuses_to_start_a_terminal_only_sign_in(daemon):
    status, body = request(daemon, "/auth/agy/start", payload={})
    assert (status, body["sign_in"]) == (409, "terminal")
    assert request(daemon, "/auth/agy/status")[1]["sign_in"] == "terminal"
