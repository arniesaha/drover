"""Focused contracts for the API/analytics process boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

from drover.config import AnalyticsBoundaryConfig, RuntimeConfig, load_config
from drover.server.analytics_boundary import (
    ANALYTICS_UNAVAILABLE_BODY,
    AnalyticsBoundaryClient,
    AnalyticsBoundaryRequestInvalid,
    AnalyticsBoundaryUnavailable,
    BoundaryResponse,
    is_analytics_route,
    start_analytics_boundary_server,
)
from drover.server.metrics import MetricsCollector
from drover.server.web.app import start_metrics_server


def test_runtime_role_defaults_to_legacy_all_and_boundary_rejects_remote_urls(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[runtime]\nrole = 'api'\n"
        "[control_store]\nbackend = 'postgres'\ndsn_env = 'DROVER_TEST_POSTGRES_DSN'\n"
        "[analytics_boundary]\nworker_url = 'http://analytics.example:7082'\n"
    )

    with pytest.raises(ValueError, match="loopback"):
        load_config(config_path)

    assert RuntimeConfig().role == "all"
    boundary = AnalyticsBoundaryConfig()
    assert boundary.worker_url == "http://127.0.0.1:7082"
    assert boundary.connect_timeout_seconds == 0.25
    assert boundary.request_timeout_seconds == 10.0
    assert boundary.max_request_bytes == 262_144
    assert boundary.max_response_bytes == 4_194_304


def test_analytics_boundary_has_fixed_routes_and_strips_public_credentials() -> None:
    seen: dict[str, object] = {}

    def transport(method: str, url: str, headers: dict[str, str], body: bytes):
        seen.update(method=method, url=url, headers=headers, body=body)
        return BoundaryResponse(200, "application/json", b'{"ready":true}\n')

    client = AnalyticsBoundaryClient(
        AnalyticsBoundaryConfig(), token="boundary-secret", transport=transport
    )
    response = client.request(
        "GET",
        "/insights",
        "limit=10",
        b"",
        public_headers={"Authorization": "Bearer public", "Cookie": "sid=x"},
    )

    assert response.status == 200
    assert seen["headers"] == {
        "Accept": "application/json",
        "X-Drover-Api-To-Analytics": "boundary-secret",
    }
    assert is_analytics_route("GET", "/insights", "limit=10") is True
    assert is_analytics_route("GET", "/insights/near/match", "") is False
    with pytest.raises(AnalyticsBoundaryRequestInvalid):
        client.request("POST", "/metrics", "", b"{}")


def test_analytics_boundary_maps_bad_worker_results_to_one_unavailable_response() -> (
    None
):
    def transport(*_args: object, **_kwargs: object) -> BoundaryResponse:
        return BoundaryResponse(200, "application/json", b"x" * (4_194_304 + 1))

    client = AnalyticsBoundaryClient(
        AnalyticsBoundaryConfig(), token="boundary-secret", transport=transport
    )

    with pytest.raises(AnalyticsBoundaryUnavailable):
        client.request("GET", "/observability", "", b"")

    assert json.loads(ANALYTICS_UNAVAILABLE_BODY) == {
        "error": "analytics worker unavailable"
    }


def test_harness_snapshot_uses_registered_control_store_without_lake_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeHost:
        def __init__(self) -> None:
            self.host_id = "host-1"
            self.display_name = "Host one"
            self.kind = "mac"
            self.status = "online"
            self.connection_kind = "direct"
            self.last_seen_at = None
            self.capabilities = {}

    class Registry:
        def __init__(self, path: Path) -> None:
            assert path == tmp_path / "absent-analytics.duckdb"

        def list_hosts(self):
            return [FakeHost()]

        def list_sessions(self, **_kwargs: object):
            return []

        def latest_session_previews(self, _ids: list[str]):
            return {}

        def latest_live_recaps(self, _ids: list[str]):
            return {}

    monkeypatch.setattr("drover.server.metrics.HarnessRegistry", Registry)
    collector = MetricsCollector(
        duckdb_path=tmp_path / "absent-analytics.duckdb",
        incoming_dir=tmp_path / "unreadable-incoming",
        summarizer_report={},
    )

    snapshot = collector.harness_snapshot()

    assert snapshot["hosts"][0]["host_id"] == "host-1"
    assert "error" not in snapshot


def test_api_listener_keeps_harness_local_when_analytics_boundary_is_down(
    tmp_path: Path,
) -> None:
    class Collector:
        duckdb_path = tmp_path / "central-selector"

        def render_harness_json(self, **_kwargs: object) -> str:
            return '{"hosts":[{"host_id":"central-host"}]}\n'

    def unavailable(*_args: object, **_kwargs: object) -> BoundaryResponse:
        raise OSError("worker is down")

    boundary = AnalyticsBoundaryClient(
        AnalyticsBoundaryConfig(), token="api-worker-secret", transport=unavailable
    )
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=Collector(),  # type: ignore[arg-type]
        analytics_boundary=boundary,
    )
    try:
        host, port = server.server_address[:2]
        connection = HTTPConnection(host, port, timeout=2)
        connection.request("GET", "/harness")
        harness = connection.getresponse()
        assert harness.status == 200
        assert json.loads(harness.read()) == {"hosts": [{"host_id": "central-host"}]}
        connection.request("GET", "/metrics")
        analytics = connection.getresponse()
        assert analytics.status == 503
        assert analytics.getheader("Retry-After") == "2"
        assert json.loads(analytics.read()) == {"error": "analytics worker unavailable"}
    finally:
        server.shutdown()
        server.server_close()


def test_api_role_starts_only_control_plane_and_never_bootstraps_the_lake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """API startup must not create, stat, or warm an analytical DuckDB path."""
    from drover.config import default_config
    from drover.server import __main__ as server_main

    calls: list[str] = []

    class Stop:
        def set(self) -> None:
            return None

        def wait(self) -> bool:
            return True

    class Server:
        def shutdown(self) -> None:
            calls.append("shutdown")

        def server_close(self) -> None:
            calls.append("server_close")

    class Consent:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            calls.append("consent_construct")

        def initialize(self, _config: object) -> None:
            calls.append("consent_initialize")

        def state(self):
            return SimpleNamespace(heartbeat=lambda: {"enabled": False, "epoch": 0})

    class Collector:
        def __init__(self, **kwargs: object) -> None:
            calls.append("collector")
            assert kwargs["include_analytical_readiness"] is False
            assert kwargs["advisory_service"] if "advisory_service" in kwargs else True

    monkeypatch.setattr(
        server_main,
        "bootstrap_control_plane_store",
        lambda _path: calls.append("control_bootstrap"),
    )
    monkeypatch.setattr(
        server_main,
        "require_control_store_ready",
        lambda _path: calls.append("control_ready"),
    )
    monkeypatch.setattr(server_main, "CentralContentConsent", Consent)
    monkeypatch.setattr(server_main, "MetricsCollector", Collector)
    monkeypatch.setattr(
        server_main,
        "load_auth",
        lambda _cfg: SimpleNamespace(enabled=False, api_token=""),
    )
    monkeypatch.setattr(server_main, "_configure_push", lambda *_args: None)
    monkeypatch.setattr(server_main, "start_metrics_server", lambda **_kwargs: Server())
    monkeypatch.setattr(server_main.threading, "Event", Stop)
    monkeypatch.setattr(server_main.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        server_main,
        "bootstrap",
        lambda **_kwargs: pytest.fail("API role must not bootstrap analytics"),
    )
    monkeypatch.setattr(
        server_main,
        "close_control_plane_connections",
        lambda: calls.append("close_control"),
    )

    server_main._run_api_role(
        cfg=replace(default_config(), update_enabled=False),
        config_path=tmp_path / "config.toml",
        metrics_host="127.0.0.1",
    )

    assert calls == [
        "control_bootstrap",
        "control_ready",
        "consent_construct",
        "consent_initialize",
        "collector",
        "shutdown",
        "server_close",
        "close_control",
    ]


def test_archive_payload_rpc_is_typed_and_fails_closed() -> None:
    payload = '{"text":"cold history"}'
    import hashlib

    payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
    seen: dict[str, object] = {}

    def transport(method: str, url: str, headers: dict[str, str], body: bytes):
        seen.update(method=method, url=url, headers=headers, body=json.loads(body))
        return BoundaryResponse(
            200, "application/json", json.dumps({"payload_json": payload}).encode()
        )

    resolver = AnalyticsBoundaryClient(
        AnalyticsBoundaryConfig(), token="boundary-secret", transport=transport
    )
    assert (
        resolver.resolve(
            event_id="event-1", batch_id="batch-1", payload_sha256=payload_sha256
        )
        == payload
    )
    assert seen["method"] == "POST"
    assert seen["url"] == "http://127.0.0.1:7082/_internal/analytics/archive-payload"
    assert seen["headers"] == {
        "Accept": "application/json",
        "X-Drover-Api-To-Analytics": "boundary-secret",
    }

    bad = AnalyticsBoundaryClient(
        AnalyticsBoundaryConfig(),
        token="boundary-secret",
        transport=lambda *_args: BoundaryResponse(
            200, "application/json", b'{"payload_json":"wrong"}'
        ),
    )
    assert (
        bad.resolve(
            event_id="event-1", batch_id="batch-1", payload_sha256=payload_sha256
        )
        is None
    )


def test_cold_history_page_has_one_aggregate_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page cannot spend one full worker timeout on every archived event."""
    import hashlib

    from drover.server import analytics_boundary

    payload = '{"text":"cold"}'
    digest = hashlib.sha256(payload.encode()).hexdigest()
    calls: list[str] = []

    def transport(_method: str, _url: str, _headers: dict[str, str], _body: bytes):
        calls.append("worker")
        return BoundaryResponse(
            200, "application/json", json.dumps({"payload_json": payload}).encode()
        )

    clock = iter((0.0, 0.0, 0.0, 0.2))
    monkeypatch.setattr(analytics_boundary.time, "monotonic", lambda: next(clock))
    client = AnalyticsBoundaryClient(
        AnalyticsBoundaryConfig(
            request_timeout_seconds=0.1, connect_timeout_seconds=0.1
        ),
        token="boundary-secret",
        transport=transport,
    )
    resolver = client.page_resolver()
    assert (
        resolver.resolve(event_id="event-1", batch_id="batch-1", payload_sha256=digest)
        == payload
    )
    assert (
        resolver.resolve(event_id="event-2", batch_id="batch-2", payload_sha256=digest)
        is None
    )
    assert calls == ["worker"]


def test_worker_listener_authenticates_health_and_typed_archive_lookup() -> None:
    """The internal listener exposes only typed worker operations on loopback."""
    import hashlib

    payload = '{"text":"verified cold history"}'
    digest = hashlib.sha256(payload.encode()).hexdigest()
    base = AnalyticsBoundaryConfig()
    server = start_analytics_boundary_server(
        host="127.0.0.1",
        port=0,
        token="api-worker-secret",
        config=base,
        dispatch=lambda *_args: BoundaryResponse(
            200, "application/json", b'{"ok":true}\n'
        ),
        archive_payload_resolver=lambda event_id, batch_id, payload_sha256: (
            payload
            if (event_id, batch_id, payload_sha256) == ("event-1", "batch-1", digest)
            else None
        ),
        health_provider=lambda: {"state": "ok", "outbox": {"pending": 0}},
    )
    try:
        host, port = server.server_address[:2]
        config = replace(base, worker_url=f"http://{host}:{port}")
        client = AnalyticsBoundaryClient(config, token="api-worker-secret")
        assert client.health_state() == "ok"
        assert (
            client.resolve(
                event_id="event-1", batch_id="batch-1", payload_sha256=digest
            )
            == payload
        )

        unauthenticated = HTTPConnection(host, port, timeout=2)
        unauthenticated.request("GET", "/_internal/analytics/health")
        assert unauthenticated.getresponse().status == 401
        unauthenticated.close()
    finally:
        server.shutdown()
        server.server_close()
