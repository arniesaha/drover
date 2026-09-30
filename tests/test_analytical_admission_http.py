"""Injected slow work must not queue control requests behind analytics (#331)."""

import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from drover.schema import bootstrap
from drover.server.analytics_maintenance import AnalyticalMaintenanceGate
from drover.server.cockpit.service import CockpitService
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import HarnessRenderBusy, MetricsCollector
from drover.server.web.app import analytics_boundary_dispatcher, start_metrics_server


def request(port, path, method="GET"):
    started = time.monotonic()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        response = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        result = response.status, json.load(response), response.headers
    return result, time.monotonic() - started


@pytest.fixture
def collector(tmp_path):
    return MetricsCollector(
        duckdb_path=tmp_path / "analytics.duckdb",
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        cockpit_service=CockpitService(
            duckdb_path=None,
            provider_usage=None,
            maintenance_gate=AnalyticalMaintenanceGate(),
        ),
    )


def test_long_analytics_keeps_fleet_fast_and_rejects_excess_work(
    collector, monkeypatch
):
    monkeypatch.setenv("DROVER_ANALYTICAL_HTTP_CONCURRENCY", "1")
    entered, release = threading.Event(), threading.Event()

    def slow(filters):
        entered.set()
        assert release.wait(10)
        return 200, "{}"

    monkeypatch.setattr(collector, "render_analytics_json", slow)
    bootstrap(
        parquet_dir=collector.duckdb_path.parent / "parquet",
        duckdb_path=collector.duckdb_path,
    )
    registry = HarnessRegistry(collector.duckdb_path)
    registry.register_host(
        host_id="studio",
        display_name="Studio",
        kind="macos",
        connection_kind="relay",
        status="offline",
    )
    session = registry.create_session(
        host_id="studio", harness="codex", command="codex", status="running"
    )
    server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    port = server.server_address[1]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(request, port, "/analytics")
            try:
                assert entered.wait(2)
                gate = collector.cockpit_service.maintenance_gate
                assert not gate.try_begin_maintenance()
                from drover.server.harness.usage_rollup import UsageRollupWorker
                from drover.server.native_usage_rollup import NativeUsageRollupWorker

                workers = [
                    UsageRollupWorker(
                        duckdb_path=collector.duckdb_path, maintenance_gate=gate
                    ),
                    NativeUsageRollupWorker(
                        duckdb_path=collector.duckdb_path, maintenance_gate=gate
                    ),
                ]
                for worker in workers:
                    for _ in range(25):
                        worker.drain_once()
                    assert worker.deferred_passes == 25
                for path in ("/harness", "/harness/hosts"):
                    (status, body, _), elapsed = request(port, path)
                    assert status == 200
                    assert body["hosts"][0]["host_id"] == "studio"
                    if path == "/harness":
                        assert body["sessions"][0]["session_id"] == session.session_id
                    assert elapsed < 1
                for path, method in (
                    ("/analytics", "GET"),
                    ("/cockpit/overview", "GET"),
                    ("/insights", "GET"),
                    ("/insights/content-analysis/consent", "POST"),
                    ("/insights/content-excerpts", "DELETE"),
                ):
                    (status, body, headers), elapsed = request(port, path, method)
                    assert status == 503
                    assert body["error"] == "analytics busy"
                    assert headers["Retry-After"] == "1"
                    assert elapsed < 1
            finally:
                release.set()
            assert pending.result()[0][0] == 200
        assert request(port, "/analytics")[0][0] == 200
        assert gate.try_begin_maintenance()
        gate.end_maintenance()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "path", ["/harness", "/harness/hosts", "/harness?archived=100"]
)
def test_fleet_saturation_is_fast_even_for_custom_listing(collector, monkeypatch, path):
    monkeypatch.setenv("DROVER_FLEET_HTTP_CONCURRENCY", "1")
    entered, release = threading.Event(), threading.Event()

    def slow(**kwargs):
        entered.set()
        assert release.wait(10)
        return "{}"

    monkeypatch.setattr(collector, "render_harness_json", slow)
    server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    port = server.server_address[1]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(request, port, path)
            try:
                assert entered.wait(2)
                (status, body, headers), elapsed = request(port, path)
                assert status == 503
                assert body["error"] == "fleet listing busy"
                assert headers["Retry-After"] == "1"
                assert elapsed < 1
            finally:
                release.set()
            assert pending.result()[0][0] == 200
        assert request(port, path)[0][0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_hosts_render_busy_returns_retry_header_and_releases_slot(
    collector, monkeypatch
):
    def busy(**kwargs):
        raise HarnessRenderBusy("still rendering")

    monkeypatch.setattr(collector, "render_harness_json", busy)
    monkeypatch.setenv("DROVER_FLEET_HTTP_CONCURRENCY", "1")
    server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    try:
        port = server.server_address[1]
        (status, _, headers), elapsed = request(port, "/harness/hosts")
        assert status == 503
        assert headers["Retry-After"] == "2"
        assert elapsed < 1
        monkeypatch.setattr(collector, "render_harness_json", lambda **kw: "{}")
        assert request(port, "/harness/hosts")[0][0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_worker_boundary_also_rejects_concurrent_analytics(collector, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow(filters):
        entered.set()
        assert release.wait(10)
        return 200, "{}"

    monkeypatch.setattr(collector, "render_analytics_json", slow)
    monkeypatch.setenv("DROVER_ANALYTICAL_HTTP_CONCURRENCY", "1")
    dispatch = analytics_boundary_dispatcher(collector)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(dispatch, "GET", "/analytics", "", b"")
        try:
            assert entered.wait(2)
            started = time.monotonic()
            assert dispatch("GET", "/analytics", "", b"").status == 503
            assert time.monotonic() - started < 1
        finally:
            release.set()
        assert pending.result().status == 200
    assert dispatch("GET", "/analytics", "", b"").status == 200
