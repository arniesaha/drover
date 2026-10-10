"""Operator host lifecycle preserves history while removing fleet capacity."""

import json
import urllib.error
import urllib.request

import pytest
from click.testing import CliRunner

from drover.schema import bootstrap
from drover.server.harness.registry import (
    HarnessRegistry,
    HostBusyError,
    HostRetiredError,
)
from drover.server.metrics import MetricsCollector
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings
from drover.server.web.credentials import CredentialStore


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    registry = HarnessRegistry(path)
    registry.register_host(host_id="mac-mini", display_name="Original Mac", kind="mac")
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    return registry, collector


def test_retire_preserves_history_and_rejects_heartbeat(fleet):
    registry, collector = fleet
    session = registry.create_session(
        host_id="mac-mini", harness="claude", command="claude", status="completed"
    )
    collector.render_harness_json(include_sessions=False)  # prime fleet cache
    assert (
        collector.retire_harness_host("mac-mini", {"reason": "uninstalled"})[0] == 200
    )
    assert registry.list_hosts() == []
    host = registry.list_hosts(include_retired=True)[0]
    assert host.retired_at and host.retired_reason == "uninstalled"
    assert host.display_name == "Original Mac"
    assert registry.get_session(session.session_id).host_id == host.host_id
    history = collector.harness_snapshot()["sessions"][0]
    assert history["host_display_name"] == "Original Mac"
    assert history["host_retired_at"]
    assert (
        json.loads(collector.render_harness_json(include_sessions=False))["hosts"] == []
    )
    assert collector.register_harness_host(
        {"host_id": "mac-mini", "display_name": "Changed"}
    ) == (409, '{"error": "host retired; unretire to rejoin"}\n')
    assert registry.get_host("mac-mini").display_name == "Original Mac"
    with pytest.raises(HostRetiredError):
        registry.create_session(host_id="mac-mini", harness="claude", command="claude")
    registry.unretire_host("mac-mini")
    assert collector.register_harness_host({"host_id": "mac-mini"})[0] == 200


@pytest.mark.parametrize(
    "status,awaiting", [("running", None), ("awaiting", None), ("idle", "input")]
)
def test_busy_guard_and_force(fleet, status, awaiting):
    registry, _ = fleet
    session = registry.create_session(
        host_id="mac-mini", harness="claude", command="claude", status=status
    )
    if awaiting:
        with registry._connect() as con:
            con.execute(
                "UPDATE harness_sessions SET awaiting = ? WHERE session_id = ?",
                [awaiting, session.session_id],
            )
    with pytest.raises(HostBusyError):
        registry.retire_host("mac-mini", reason="gone")
    assert registry.get_host("mac-mini").retired_at is None
    registry.retire_host("mac-mini", reason="gone", force=True)
    with pytest.raises(HostRetiredError, match="host retired"):
        registry.register_host(host_id="mac-mini", display_name="Mac", kind="mac")


def test_api_operator_revocation_and_visibility(fleet, tmp_path):
    registry, collector = fleet
    store = CredentialStore(tmp_path / "credentials.json")
    host_credential, host_token = store.issue(
        scope="host", label="Mac", host_id="mac-mini"
    )
    _, device_token = store.issue(scope="device", label="Phone")
    auth = AuthSettings(enabled=True, api_token="operator", credentials=store)
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=auth
    )

    def call(method, path, body=None, token="operator"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}" + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        assert (
            call(
                "POST",
                "/harness/hosts/mac-mini/retire",
                {"reason": "gone"},
                device_token,
            )[0]
            == 403
        )
        assert (
            call(
                "POST", "/harness/hosts/mac-mini/retire", {"reason": "gone"}, host_token
            )[0]
            == 403
        )
        assert call("POST", "/harness/hosts/mac-mini/retire", {})[0] == 400
        assert (
            call("POST", "/harness/hosts/unknown/retire", {"reason": "gone"})[0] == 404
        )
        assert (
            call("POST", "/harness/hosts/mac-mini/retire", {"reason": "gone"})[0] == 200
        )
        assert store.find_active(host_token) is None
        assert call("GET", "/harness/hosts")[1]["hosts"] == []
        assert call("GET", "/harness/hosts?include_retired=1")[1]["hosts"][0][
            "retired_at"
        ]
        # The operator bearer cannot impersonate a host, even after retirement.
        assert call("POST", "/harness/hosts/mac-mini/heartbeat", {})[0] == 403
        assert call("POST", "/harness/hosts/mac-mini/unretire", {})[0] == 200
        assert store.find_active(host_token) is None
    finally:
        server.shutdown()
        server.server_close()


def test_cli_lifecycle(monkeypatch):
    from drover.server import __main__ as cli

    calls = []
    monkeypatch.setattr(cli, "_resolve_config", lambda *args, **kwargs: object())

    def request(cfg, method, path, payload=None):
        calls.append((method, path, payload))
        return {
            "hosts": [
                {
                    "host_id": "mac-mini",
                    "status": "stale",
                    "last_seen_at": "2026-09-27",
                    "retired_at": "2026-10-01",
                }
            ]
        }

    monkeypatch.setattr(cli, "_local_api_request", request)
    runner = CliRunner()
    result = runner.invoke(cli.main, ["hosts", "list"])
    assert (
        result.exit_code == 0
        and "2026-09-27" in result.output
        and "2026-10-01" in result.output
    )
    assert (
        runner.invoke(
            cli.main, ["hosts", "retire", "mac-mini", "--reason", "gone"], input="n\n"
        ).exit_code
        == 1
    )
    assert len(calls) == 1
    assert (
        runner.invoke(
            cli.main,
            ["hosts", "retire", "mac-mini", "--reason", "gone", "--yes", "--force"],
        ).exit_code
        == 0
    )
    assert calls[-1] == (
        "POST",
        "/harness/hosts/mac-mini/retire",
        {"reason": "gone", "force": True},
    )
    assert runner.invoke(cli.main, ["hosts", "unretire", "mac-mini"]).exit_code == 0
    assert calls[-1] == ("POST", "/harness/hosts/mac-mini/unretire", {})


def test_cached_capacity_excludes_retired_hosts(fleet):
    from drover.server.cockpit.service import CockpitService

    registry, collector = fleet
    service = CockpitService(duckdb_path=collector.duckdb_path, provider_usage=None)
    service._provider_cache = (
        (None, None),
        {
            "data": [{"host_id": "mac-mini"}, {"host_id": "nas"}],
            "coverage": {"account_count": 2},
        },
    )
    registry.retire_host("mac-mini", reason="uninstalled")
    section = service._last_good_provider_capacity((None, None))
    assert section["data"] == [{"host_id": "nas"}]
    assert section["coverage"]["account_count"] == 1
