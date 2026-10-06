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
        response = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        body = (
            json.loads(raw)
            if response.headers.get_content_type() == "application/json"
            else raw.decode()
        )
        result = response.status, body, response.headers
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
                from drover.server.native_usage_rollup import NativeUsageRollupWorker

                worker = NativeUsageRollupWorker(
                    duckdb_path=collector.duckdb_path, maintenance_gate=gate
                )
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
                    assert 0.9 <= elapsed < 2
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
            assert 0.9 <= time.monotonic() - started < 2
        finally:
            release.set()
        assert pending.result().status == 200
    assert dispatch("GET", "/analytics", "", b"").status == 200


@pytest.fixture(params=[False, True], ids=["all-in-one", "split"])
def analytical_server(collector, request):
    from drover.config import AnalyticsBoundaryConfig
    from drover.server.analytics_boundary import (
        AnalyticsBoundaryClient,
        start_analytics_boundary_server,
    )

    worker = None
    boundary = None
    if request.param:
        config = AnalyticsBoundaryConfig(max_concurrent_requests=1)
        worker = start_analytics_boundary_server(
            host="127.0.0.1",
            port=0,
            token="test-token",
            config=config,
            dispatch=analytics_boundary_dispatcher(collector),
        )
        from dataclasses import replace

        config = replace(
            config, worker_url=f"http://127.0.0.1:{worker.server_address[1]}"
        )
        boundary = AnalyticsBoundaryClient(config, token="test-token")
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, analytics_boundary=boundary
    )
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        if worker is not None:
            worker.shutdown()
            worker.server_close()


def test_scrapes_and_small_mutations_bypass_heavy_build(
    collector, monkeypatch, analytical_server
):
    entered, release = threading.Event(), threading.Event()

    def slow(filters):
        entered.set()
        assert release.wait(10)
        return 200, "{}"

    monkeypatch.setattr(collector, "render_cockpit_overview_json", slow)
    monkeypatch.setattr(collector, "render_prometheus", lambda: "scrape_ok 1\n")
    monkeypatch.setattr(
        collector,
        "act_on_insight",
        lambda finding_id, action, body: (200, json.dumps({"action": action})),
    )
    port = analytical_server
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(request, port, "/cockpit/overview")
        try:
            assert entered.wait(2)
            (status, body, _), elapsed = request(port, "/metrics")
            assert status == 200
            assert body == "scrape_ok 1\n"
            assert elapsed < 1
            for action in ("acknowledge", "dismiss", "check"):
                (status, body, _), elapsed = request(
                    port, f"/insights/{'a' * 32}/{action}", "POST"
                )
                assert status == 200
                assert body == {"action": action}
                assert elapsed < 1
        finally:
            release.set()
        assert pending.result()[0][0] == 200


def test_heavy_request_waits_briefly_for_released_slot(
    collector, monkeypatch, analytical_server
):
    entered, release = threading.Event(), threading.Event()

    def slow(filters):
        entered.set()
        assert release.wait(10)
        return 200, "{}"

    monkeypatch.setattr(collector, "render_analytics_json", slow)
    port = analytical_server
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(request, port, "/analytics")
        timer = threading.Timer(0.2, release.set)
        try:
            assert entered.wait(2)
            timer.start()
            (status, _, _), elapsed = request(port, "/analytics")
            assert status == 200
            assert elapsed < 2
        finally:
            release.set()
            timer.cancel()
        assert first.result()[0][0] == 200


@pytest.mark.parametrize("lane,capacity", [("metrics", 2), ("mutation", 4)])
def test_exempt_routes_keep_independent_bounded_capacity(
    collector, monkeypatch, analytical_server, lane, capacity
):
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls = 0

    def slow(*args):
        nonlocal calls
        with lock:
            calls += 1
            if calls == capacity:
                entered.set()
        assert release.wait(10)
        return "scrape_ok 1\n" if lane == "metrics" else (200, "{}")

    if lane == "metrics":
        monkeypatch.setattr(collector, "render_prometheus", slow)
        path, method = "/metrics", "GET"
    else:
        monkeypatch.setattr(collector, "act_on_insight", slow)
        path, method = f"/insights/{'a' * 32}/acknowledge", "POST"
    monkeypatch.setattr(collector, "render_analytics_json", lambda _: (200, "{}"))
    port = analytical_server
    with ThreadPoolExecutor(max_workers=capacity) as pool:
        pending = [pool.submit(request, port, path, method) for _ in range(capacity)]
        try:
            assert entered.wait(3)
            (status, _, headers), elapsed = request(port, path, method)
            assert status == 503
            assert int(headers["Retry-After"]) >= 1
            assert elapsed < 1
            # A stuck small request must not occupy the heavy slot either.
            assert request(port, "/analytics")[0][0] == 200
        finally:
            release.set()
        assert all(f.result()[0][0] == 200 for f in pending)
    assert request(port, path, method)[0][0] == 200


@pytest.mark.parametrize("internal", [False, True])
def test_analytical_request_releases_idle_buffers_but_health_does_not(
    collector, monkeypatch, internal
):
    from drover.server import memory

    released = []
    done = threading.Event()

    def release():
        released.append(True)
        done.set()

    monkeypatch.setattr(memory, "release_idle_arrow_memory", release)
    monkeypatch.setattr(collector, "render_analytics_json", lambda filters: (200, "{}"))
    if internal:
        dispatch = analytics_boundary_dispatcher(collector)
        assert dispatch("GET", "/analytics", "", b"").status == 200
        assert released == [True]
    else:
        server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
        try:
            port = server.server_address[1]
            assert request(port, "/healthz")[0][0] == 200
            assert released == []
            assert request(port, "/analytics")[0][0] == 200
            # The handler releases in its ``finally``, after the response is
            # on the wire, so the client can get there first (Linux CI did).
            assert done.wait(5)
            assert released == [True]
        finally:
            server.shutdown()
            server.server_close()
