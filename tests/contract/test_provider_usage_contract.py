"""Provider-usage wire contracts using only the launcher fixture's fake CLIs."""

from __future__ import annotations

from .support import wait_until
from .test_harnessd_contract import request


def test_harnessd_provider_usage_requires_auth_and_reports_unavailable_accounts(daemon):
    assert request(daemon, "/providers/usage", token=False) == (
        401,
        {"error": "authentication required"},
    )
    status, body = request(daemon, "/providers/usage")
    assert status == 200
    assert isinstance(body["accounts"], list)
    assert body["observed_at"].endswith("+00:00")
    for account in body["accounts"]:
        assert {"provider", "status", "usage_status", "windows", "host_id"} <= set(
            account
        )
        assert account["host_id"] == "contract-host"


def test_harnessd_provider_usage_probes_codex_at_its_resolved_executable(daemon):
    wait_until(
        lambda: any(
            account["provider"] == "openai" and account["status"] == "ok"
            for account in request(daemon, "/providers/usage")[1]["accounts"]
        )
    )
    status, body = request(daemon, "/providers/usage")
    assert status == 200
    codex = next(
        account for account in body["accounts"] if account["provider"] == "openai"
    )
    assert codex["status"] == "ok"
    assert codex["plan_label"] == "plus"
    assert [window["used_percent"] for window in codex["windows"]] == [25.0]


def test_harnessd_provider_usage_reports_codex_cli_not_found_is_never_silent(daemon):
    status, body = request(daemon, "/providers/usage")
    assert status == 200
    for account in body["accounts"]:
        if account["status"] == "error":
            assert account["error_category"]
