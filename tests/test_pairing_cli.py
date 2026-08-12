"""Tests for the pairing CLI commands -- URL derivation and hub round trips."""

from __future__ import annotations

import dataclasses

from click.testing import CliRunner

from drover.config import default_config
from drover.server.__main__ import _advertised_host_port, main


def test_advertised_host_port_prefers_the_configured_url():
    cfg = dataclasses.replace(
        default_config(), server_advertised_url="100.64.0.10:7080"
    )
    assert _advertised_host_port(cfg) == "100.64.0.10:7080"


def test_advertised_host_port_strips_a_scheme():
    cfg = dataclasses.replace(
        default_config(), server_advertised_url="http://100.64.0.10:7080"
    )
    assert _advertised_host_port(cfg) == "100.64.0.10:7080"


def test_advertised_host_port_falls_back_to_loopback():
    cfg = default_config()
    assert _advertised_host_port(cfg) == f"127.0.0.1:{cfg.metrics_http_port}"


def test_pair_prints_a_qr_and_warns_about_loopback(monkeypatch):
    import drover.server.__main__ as server_main

    monkeypatch.setattr(
        server_main,
        "_local_api_request",
        lambda cfg, method, path, payload=None: {
            "code": "K7QP-2M4X",
            "expires_in_seconds": 600,
            "fleet_name": "home-fleet",
        },
    )
    result = CliRunner().invoke(main, ["pair"])
    assert result.exit_code == 0, result.output
    assert "K7QP-2M4X" in result.output
    assert "drover://127.0.0.1:7080?v=1&code=K7QP-2M4X&n=home-fleet" in result.output
    assert "█" in result.output, "the QR itself must be rendered"
    assert "only reachable from this machine" in result.output


def test_pair_host_prints_a_pasteable_command(monkeypatch):
    import drover.server.__main__ as server_main

    monkeypatch.setattr(
        server_main,
        "_local_api_request",
        lambda cfg, method, path, payload=None: {
            "code": "H3TW-9KQ2",
            "expires_in_seconds": 900,
            "fleet_name": "home-fleet",
        },
    )
    result = CliRunner().invoke(main, ["pair-host", "--name", "build-mac"])
    assert result.exit_code == 0, result.output
    assert "--join" in result.output
    assert "H3TW-9KQ2" in result.output
    assert "15 min" in result.output


def test_pair_host_sends_the_host_scope(monkeypatch):
    import drover.server.__main__ as server_main

    sent = {}

    def _capture(cfg, method, path, payload=None):
        sent.update({"path": path, "payload": payload})
        return {"code": "H3TW-9KQ2", "expires_in_seconds": 900, "fleet_name": "f"}

    monkeypatch.setattr(server_main, "_local_api_request", _capture)
    CliRunner().invoke(main, ["pair-host", "--name", "build-mac"])
    assert sent["path"] == "/auth/pair-codes"
    assert sent["payload"]["scope"] == "host"
    assert sent["payload"]["host_id"] == "build-mac"


def test_credentials_list_renders_a_table(monkeypatch):
    import drover.server.__main__ as server_main

    monkeypatch.setattr(
        server_main,
        "_local_api_request",
        lambda cfg, method, path, payload=None: {
            "credentials": [
                {
                    "id": "abc-123",
                    "scope": "device",
                    "label": "Phone",
                    "created_at": "2026-08-11T00:00:00+00:00",
                    "last_used_at": None,
                    "revoked_at": None,
                    "host_id": None,
                }
            ]
        },
    )
    result = CliRunner().invoke(main, ["credentials", "list"])
    assert result.exit_code == 0, result.output
    assert "abc-123" in result.output
    assert "Phone" in result.output
    assert "active" in result.output


def test_credentials_list_says_so_when_empty(monkeypatch):
    import drover.server.__main__ as server_main

    monkeypatch.setattr(
        server_main,
        "_local_api_request",
        lambda cfg, method, path, payload=None: {"credentials": []},
    )
    result = CliRunner().invoke(main, ["credentials", "list"])
    assert "No credentials" in result.output


def test_credentials_revoke_calls_delete(monkeypatch):
    import drover.server.__main__ as server_main

    sent = {}

    def _capture(cfg, method, path, payload=None):
        sent.update({"method": method, "path": path})
        return {}

    monkeypatch.setattr(server_main, "_local_api_request", _capture)
    result = CliRunner().invoke(main, ["credentials", "revoke", "abc-123"])
    assert result.exit_code == 0, result.output
    assert sent == {"method": "DELETE", "path": "/auth/credentials/abc-123"}


def test_unreachable_hub_is_a_clear_error(monkeypatch):
    import drover.server.__main__ as server_main

    def _refuse(cfg, method, path, payload=None):
        raise server_main.click.ClickException(
            "could not reach drover-server on 127.0.0.1:7080 -- is it running?"
        )

    monkeypatch.setattr(server_main, "_local_api_request", _refuse)
    result = CliRunner().invoke(main, ["pair"])
    assert result.exit_code != 0
    assert "is it running?" in result.output
