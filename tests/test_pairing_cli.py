"""Tests for the pairing CLI commands -- URL derivation and hub round trips."""

from __future__ import annotations

import dataclasses

from click.testing import CliRunner

from drover.config import default_config
from drover.server.__main__ import _advertised_host_port, _local_api_host, main

# -- reaching the hub this machine is actually running ---------------------
#
# The CLI hardcoded 127.0.0.1 while the server binds `[server].metrics_host`,
# which install.sh sets to the address it detected for the phone. On any
# machine with a LAN address that is not loopback, so every subcommand that
# calls the hub reported "could not reach drover-server -- is it running?"
# about a server that was running and serving. This failed the published
# release's own install verification for both v0.2.0 and v0.3.0.


def test_local_api_host_uses_the_address_the_server_binds():
    cfg = dataclasses.replace(default_config(), server_metrics_host="10.1.0.73")
    assert _local_api_host(cfg) == "10.1.0.73"


def test_local_api_host_prefers_loopback_for_a_wildcard_bind():
    # A wildcard listener answers on loopback, and loopback is the better
    # choice for a local call: it does not leave the machine and does not
    # depend on which interface happens to be up.
    for wildcard in ("0.0.0.0", "::", ""):
        cfg = dataclasses.replace(default_config(), server_metrics_host=wildcard)
        assert _local_api_host(cfg) == "127.0.0.1"


def test_local_api_request_calls_the_bound_address(monkeypatch):
    import drover.server.__main__ as server_main

    cfg = dataclasses.replace(
        default_config(), server_metrics_host="10.1.0.73", auth_api_token="t"
    )
    seen: dict = {}

    class _Response:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(request, timeout=None):
        seen["url"] = request.full_url
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    server_main._local_api_request(cfg, "GET", "/auth/credentials")

    assert seen["url"] == f"http://10.1.0.73:{cfg.metrics_http_port}/auth/credentials"


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


def _pair_cli(monkeypatch, cfg):
    """Invoke `pair` against a given config rather than whatever is on disk.

    `pair` takes no --config here, so without this the command loads the real
    ~/.drover/config.toml. That made the loopback assertion below pass only on
    machines where advertised_url is unset -- which is to say, machines where
    pairing is not configured. It passed in CI and failed on every host that
    had been set up properly, which is exactly backwards.
    """
    import drover.server.__main__ as server_main

    monkeypatch.setattr(server_main, "_resolve_config", lambda path: cfg)
    monkeypatch.setattr(
        server_main,
        "_local_api_request",
        lambda cfg, method, path, payload=None: {
            "code": "K7QP-2M4X",
            "expires_in_seconds": 600,
            "fleet_name": "home-fleet",
        },
    )
    return CliRunner().invoke(main, ["pair"])


def test_pair_prints_a_qr_and_warns_about_loopback(monkeypatch):
    result = _pair_cli(monkeypatch, default_config())
    assert result.exit_code == 0, result.output
    assert "K7QP-2M4X" in result.output
    assert "drover://127.0.0.1:7080?v=1&code=K7QP-2M4X&n=home-fleet" in result.output
    assert "█" in result.output, "the QR itself must be rendered"
    assert "only reachable from this machine" in result.output


def test_pair_uses_the_configured_advertised_address(monkeypatch):
    """The configured case had no coverage at all, which is why the leak hid.

    A phone scans this QR from outside the machine, so the address the hub
    advertises is the whole point of the code; loopback is the fallback, not
    the subject.
    """
    cfg = dataclasses.replace(
        default_config(), server_advertised_url="http://100.64.0.10:7080"
    )

    result = _pair_cli(monkeypatch, cfg)

    assert result.exit_code == 0, result.output
    assert "drover://100.64.0.10:7080?v=1&code=K7QP-2M4X&n=home-fleet" in result.output
    assert "only reachable from this machine" not in result.output


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
