"""Focused contracts for the API/analytics process boundary."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from drover.config import AnalyticsBoundaryConfig, RuntimeConfig, load_config
from drover.server.analytics_boundary import (
    ANALYTICS_UNAVAILABLE_BODY,
    AnalyticsBoundaryClient,
    AnalyticsBoundaryRequestInvalid,
    AnalyticsBoundaryUnavailable,
    BoundaryResponse,
    HostDataBridgeClient,
    HostDataBridgeUnavailable,
    is_analytics_route,
    start_analytics_boundary_server,
)
from drover.server.metrics import MetricsCollector
from drover.server.web.app import start_metrics_server


class _SlowDripServer(ThreadingHTTPServer):
    """A real loopback peer that defeats per-receive socket timeouts."""

    daemon_threads = True

    def __init__(self, *, drip_stage: str, port: int = 0) -> None:
        self.drip_stage = drip_stage
        self.requests = 0
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", port), _SlowDripHandler)

    def next_request(self) -> int:
        with self._lock:
            current = self.requests
            self.requests += 1
            return current


class _SlowDripHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        server = self.server
        assert isinstance(server, _SlowDripServer)
        if server.next_request() == 0:
            self._slow_response(server.drip_stage)
            return
        self.connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 2\r\nConnection: close\r\n\r\n{}"
        )

    def _slow_response(self, stage: str) -> None:
        headers = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 30\r\nConnection: close\r\n\r\n"
        )
        chunks = headers if stage == "headers" else b"x" * 30
        try:
            if stage == "headers":
                for byte in chunks:
                    self.connection.sendall(bytes((byte,)))
                    time.sleep(0.025)
                for _ in range(30):
                    self.connection.sendall(b"x")
                    time.sleep(0.025)
            else:
                self.connection.sendall(headers)
                for byte in chunks:
                    self.connection.sendall(bytes((byte,)))
                    time.sleep(0.025)
        except OSError:
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request_json(
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, object]:
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    encoded = json.dumps(body).encode() if body is not None else None
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    if encoded is not None:
        request_headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        if not raw:
            return response.status, {}
        try:
            return response.status, json.loads(raw)
        except json.JSONDecodeError:
            return response.status, raw.decode("utf-8", errors="replace")
    finally:
        connection.close()


def _wait_for_http_process(
    process: subprocess.Popen[bytes],
    port: int,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"runtime process exited with {process.returncode}")
        try:
            status, _ = _request_json(port, "GET", path, headers=headers)
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("runtime listener did not become ready")


def _stop_runtime_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _write_split_runtime_config(
    path: Path,
    *,
    duckdb_path: Path,
    root: Path,
    schema: str,
    metrics_port: int,
    api_port: int,
    worker_port: int,
) -> None:
    quoted = json.dumps
    path.write_text(
        "\n".join(
            (
                "[paths]",
                f"incoming_dir = {quoted(str(root / 'incoming'))}",
                f"parquet_dir = {quoted(str(root / 'parquet'))}",
                f"duckdb_path = {quoted(str(duckdb_path))}",
                "",
                "[control_store]",
                'backend = "postgres"',
                'dsn_env = "DROVER_TEST_POSTGRES_DSN"',
                "pool_min_size = 1",
                "pool_max_size = 2",
                "acquire_timeout_seconds = 0.2",
                "statement_timeout_seconds = 2.0",
                f"schema = {quoted(schema)}",
                "",
                "[server]",
                f"metrics_http_port = {metrics_port}",
                'metrics_host = "127.0.0.1"',
                "",
                "[auth]",
                "enabled = true",
                'api_token = "synthetic-cluster-token"',
                "legacy_token_enabled = true",
                "",
                "[update]",
                "enabled = false",
                "",
                "[analytics_boundary]",
                f'worker_url = "http://127.0.0.1:{worker_port}"',
                f'api_url = "http://127.0.0.1:{api_port}"',
                'api_to_worker_token_env = "DROVER_API_TO_ANALYTICS_TOKEN"',
                'worker_to_api_token_env = "DROVER_ANALYTICS_TO_API_TOKEN"',
                "connect_timeout_seconds = 0.04",
                "request_timeout_seconds = 0.12",
                "max_concurrent_requests = 2",
            )
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("direction", "drip_stage"),
    (("api_to_worker", "headers"), ("worker_to_api", "body")),
)
def test_real_loopback_boundary_total_deadline_releases_slot_and_thread(
    direction: str, drip_stage: str
) -> None:
    """Headers and bodies share the original admission-to-response deadline."""

    server = _SlowDripServer(drip_stage=drip_stage)
    config = AnalyticsBoundaryConfig(
        worker_url=f"http://127.0.0.1:{server.server_port}",
        api_url=f"http://127.0.0.1:{server.server_port}",
        connect_timeout_seconds=0.04,
        request_timeout_seconds=0.12,
        max_concurrent_requests=1,
    )
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    try:
        started_at = time.monotonic()
        if direction == "api_to_worker":
            client = AnalyticsBoundaryClient(config, token="boundary-secret")
            with pytest.raises(AnalyticsBoundaryUnavailable):
                client.request("GET", "/metrics", "", b"")
            assert time.monotonic() - started_at < 0.3
            started_at = time.monotonic()
            response = client.request("GET", "/metrics", "", b"")
        else:
            client = HostDataBridgeClient(config, token="bridge-secret")
            with pytest.raises(HostDataBridgeUnavailable):
                client.fetch_provider_usage("synthetic-host")
            assert time.monotonic() - started_at < 0.3
            started_at = time.monotonic()
            response = client.fetch_provider_usage("synthetic-host")
        assert time.monotonic() - started_at < 0.3
        if direction == "api_to_worker":
            assert isinstance(response, BoundaryResponse)
            assert response.body == b"{}"
        else:
            assert response == {}
        deadline = time.monotonic() + 0.5
        while (
            any(
                thread.name == "drover-boundary-http" and thread.is_alive()
                for thread in threading.enumerate()
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert not any(
            thread.name == "drover-boundary-http" and thread.is_alive()
            for thread in threading.enumerate()
        )
    finally:
        server.shutdown()
        server.server_close()
        serve_thread.join(timeout=1)


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


def test_host_data_bridge_rejects_query_before_dispatch(tmp_path: Path) -> None:
    """The reverse bridge has no caller-selected query surface."""

    class Collector:
        duckdb_path = tmp_path / "central-control.duckdb"

    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=Collector(),  # type: ignore[arg-type]
        host_data_bridge_token="bridge-secret",
    )
    try:
        host, port = server.server_address[:2]
        connection = HTTPConnection(host, port, timeout=2)
        connection.request(
            "GET",
            "/_internal/analytics/hosts/synthetic-host/provider-usage?ignored=true",
            headers={"X-Drover-Analytics-To-Api": "bridge-secret"},
        )
        response = connection.getresponse()
        assert response.status == 404
        assert json.loads(response.read()) == {"error": "not found"}
        connection.close()
    finally:
        server.shutdown()
        server.server_close()


def test_split_roles_use_real_postgres_auth_when_lake_is_absent_and_worker_changes(
    tmp_path: Path,
) -> None:
    """API fleet serving survives lake denial, worker loss, slowness, and revoke."""

    dsn = os.environ.get("DROVER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("DROVER_TEST_POSTGRES_DSN is required for PostgreSQL integration")

    from drover.config import ControlStoreConfig
    from drover.schema import bootstrap_control_plane_store
    from drover.server.control_migration import initialize_empty_control_store
    from drover.server.control_store import close_control_store, configure_control_store
    from drover.server.harness.registry import HarnessRegistry

    schema = f"drover_runtime_{uuid4().hex}"
    api_port, worker_port, worker_metrics_port = (
        _free_loopback_port(),
        _free_loopback_port(),
        _free_loopback_port(),
    )
    lake_guard = tmp_path / "lake-guard"
    lake_guard.write_text("API must never open this analytical path", encoding="utf-8")
    api_lake = lake_guard / "analytics.duckdb"
    worker_lake = tmp_path / "worker" / "analytics.duckdb"
    api_config = tmp_path / "api.toml"
    worker_config = tmp_path / "worker.toml"
    _write_split_runtime_config(
        api_config,
        duckdb_path=api_lake,
        root=tmp_path / "api-runtime",
        schema=schema,
        metrics_port=api_port,
        api_port=api_port,
        worker_port=worker_port,
    )
    _write_split_runtime_config(
        worker_config,
        duckdb_path=worker_lake,
        root=tmp_path / "worker-runtime",
        schema=schema,
        metrics_port=worker_metrics_port,
        api_port=api_port,
        worker_port=worker_port,
    )
    control_config = ControlStoreConfig(
        backend="postgres",
        dsn_env="DROVER_TEST_POSTGRES_DSN",
        pool_min_size=1,
        pool_max_size=2,
        acquire_timeout_seconds=0.2,
        statement_timeout_seconds=2.0,
        schema=schema,
    )
    configure_control_store(api_lake, control_config)
    bootstrap_control_plane_store(api_lake)
    initialize_empty_control_store(api_lake)
    registry = HarnessRegistry(api_lake)
    registry.register_host(
        host_id="synthetic-host",
        display_name="Synthetic host",
        kind="test",
        status="offline",
    )
    registry.create_session(
        host_id="synthetic-host",
        harness="codex",
        command="synthetic",
        session_id="synthetic-session",
    )
    close_control_store(api_lake)

    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "DROVER_TEST_POSTGRES_DSN": dsn,
        "DROVER_API_TOKEN": "synthetic-cluster-token",
        "DROVER_API_TO_ANALYTICS_TOKEN": "synthetic-api-worker-token",
        "DROVER_ANALYTICS_TO_API_TOKEN": "synthetic-worker-api-token",
    }
    api_log = (tmp_path / "api.log").open("wb")
    worker_log = (tmp_path / "worker.log").open("wb")
    api_process: subprocess.Popen[bytes] | None = None
    worker_process: subprocess.Popen[bytes] | None = None
    slow_server: _SlowDripServer | None = None
    slow_thread: threading.Thread | None = None
    try:
        api_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "drover.server",
                "--config",
                str(api_config),
                "run",
                "--role",
                "api",
            ],
            stdin=subprocess.DEVNULL,
            stdout=api_log,
            stderr=subprocess.STDOUT,
            env=environment,
        )
        _wait_for_http_process(api_process, api_port, "/healthz")

        status, payload = _request_json(
            api_port,
            "POST",
            "/auth/pair-codes",
            token="synthetic-cluster-token",
            body={"scope": "device", "label": "Synthetic device"},
        )
        assert status == 201
        assert isinstance(payload, dict)
        status, payload = _request_json(
            api_port,
            "POST",
            "/auth/pair",
            token="synthetic-cluster-token",
            body={"code": payload["code"], "device_name": "Synthetic device"},
        )
        assert status == 201
        assert isinstance(payload, dict)
        device_token = payload["token"]
        credential_id = payload["credential_id"]
        assert isinstance(device_token, str)
        assert isinstance(credential_id, str)

        status, snapshot = _request_json(
            api_port, "GET", "/harness", token=device_token
        )
        assert status == 200
        assert {host["host_id"] for host in snapshot["hosts"]} == {"synthetic-host"}
        assert {session["session_id"] for session in snapshot["sessions"]} == {
            "synthetic-session"
        }
        status, _ = _request_json(api_port, "GET", "/readyz", token=device_token)
        assert status == 200

        # No worker owns the boundary yet. Fleet serving remains available.
        status, _ = _request_json(api_port, "GET", "/metrics", token=device_token)
        assert status == 503
        assert _request_json(api_port, "GET", "/harness", token=device_token)[0] == 200

        slow_server = _SlowDripServer(drip_stage="body", port=worker_port)
        slow_thread = threading.Thread(target=slow_server.serve_forever, daemon=True)
        slow_thread.start()
        started_at = time.monotonic()
        status, _ = _request_json(api_port, "GET", "/metrics", token=device_token)
        assert status == 503
        assert time.monotonic() - started_at < 0.5
        assert _request_json(api_port, "GET", "/harness", token=device_token)[0] == 200
        slow_server.shutdown()
        slow_server.server_close()
        slow_thread.join(timeout=1)
        slow_server = None
        slow_thread = None

        worker_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "drover.server",
                "--config",
                str(worker_config),
                "run",
                "--role",
                "analytics",
                "--no-otlp",
                "--no-mcp",
                "--no-summarizer",
                "--no-briefs",
                "--no-embeddings",
            ],
            stdin=subprocess.DEVNULL,
            stdout=worker_log,
            stderr=subprocess.STDOUT,
            env=environment,
        )
        _wait_for_http_process(
            worker_process,
            worker_port,
            "/_internal/analytics/health",
            headers={"X-Drover-Api-To-Analytics": "synthetic-api-worker-token"},
        )

        revoker = """
import os
from pathlib import Path
from drover.config import ControlStoreConfig
from drover.server.control_store import close_control_store, configure_control_store
from drover.server.web.credentials import credential_store_for_control_path
path = Path(os.environ['DROVER_TEST_CONTROL_PATH'])
config = ControlStoreConfig(
    backend='postgres', dsn_env='DROVER_TEST_POSTGRES_DSN',
    pool_min_size=1, pool_max_size=2, acquire_timeout_seconds=0.2,
    statement_timeout_seconds=2.0, schema=os.environ['DROVER_TEST_CONTROL_SCHEMA'],
)
configure_control_store(path, config)
try:
    assert credential_store_for_control_path(
        path, path.with_name('synthetic-credentials.json')
    ).revoke(os.environ['DROVER_TEST_CREDENTIAL_ID'])
finally:
    close_control_store(path)
"""
        revoke_env = {
            **environment,
            "DROVER_TEST_CONTROL_PATH": str(api_lake),
            "DROVER_TEST_CONTROL_SCHEMA": schema,
            "DROVER_TEST_CREDENTIAL_ID": credential_id,
        }
        revoked = subprocess.run(
            [sys.executable, "-c", revoker],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=revoke_env,
        )
        assert revoked.returncode == 0, revoked.stderr
        assert _request_json(api_port, "GET", "/harness", token=device_token)[0] == 401
    finally:
        if slow_server is not None:
            slow_server.shutdown()
            slow_server.server_close()
        if slow_thread is not None:
            slow_thread.join(timeout=1)
        _stop_runtime_process(worker_process)
        _stop_runtime_process(api_process)
        api_log.close()
        worker_log.close()
        close_control_store(api_lake)
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_harnessd_keeps_local_duckdb_spool_when_postgres_dsn_is_inherited(
    tmp_path: Path,
) -> None:
    """The host daemon never registers a hub's PostgreSQL config for its ledger."""

    dsn = os.environ.get("DROVER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("DROVER_TEST_POSTGRES_DSN is required for PostgreSQL integration")

    from drover.config import load_config
    from drover.server.control_store import is_postgres_control_store
    from drover.server.harness.cli import bootstrap_harnessd_schema
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.harness.structured.pusher import (
        EventPusher,
        reconcile_unsent_events,
    )

    config_path = tmp_path / "host.toml"
    local_lake = tmp_path / "host" / "local.duckdb"
    _write_split_runtime_config(
        config_path,
        duckdb_path=local_lake,
        root=tmp_path / "host-runtime",
        schema=f"unused_{uuid4().hex}",
        metrics_port=_free_loopback_port(),
        api_port=_free_loopback_port(),
        worker_port=_free_loopback_port(),
    )
    cfg = load_config(config_path)
    assert is_postgres_control_store(local_lake) is False
    assert bootstrap_harnessd_schema(cfg) is True
    assert is_postgres_control_store(local_lake) is False
    assert local_lake.exists()

    registry = HarnessRegistry(local_lake)
    registry.register_host(host_id="local-host", display_name="Local", kind="test")
    registry.create_session(
        host_id="local-host",
        harness="codex",
        command="synthetic",
        session_id="local-session",
        mode="structured",
    )
    registry.append_event(
        session_id="local-session",
        event_type="status",
        payload={"turn_complete": True},
        harness="codex",
        normalized_source="structured",
        event_id="local-event",
        seq=1,
    )
    outage_port = _free_loopback_port()
    pusher = EventPusher(f"http://127.0.0.1:{outage_port}", "synthetic-token")
    assert reconcile_unsent_events(registry, pusher, host_id="local-host") is None
    assert [event.event_id for event in registry.list_events_for_reconciliation()] == [
        "local-event"
    ]


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
