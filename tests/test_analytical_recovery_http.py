"""A poisoned analytical store must not strand HTTP requests (#363)."""

import json
import threading
import time
import urllib.error
import urllib.request

import duckdb
import pytest

from drover.server import db
from drover.server.cockpit.service import CockpitService
from drover.server.metrics import MetricsCollector, start_metrics_server
from drover.server.web.app import analytics_boundary_dispatcher


def request(port, path, method="GET"):
    started = time.monotonic()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        response = urllib.request.urlopen(req, timeout=2)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        result = response.status, json.load(response), response.headers
    assert time.monotonic() - started < 1
    return result


def test_detection_health_and_fast_http_errors_until_recovered(tmp_path, monkeypatch):
    path = tmp_path / "analytics.duckdb"
    con = db.open_duckdb_connection(path)
    original = con._inner
    entered, release = threading.Event(), threading.Event()
    reset = db.reset_invalidated_instance

    def held_reset(path):
        entered.set()
        assert release.wait(10)
        return reset(path)

    class Poisoned:
        def execute(self, *args):
            raise duckdb.FatalException("database has been invalidated: checkpoint OOM")

        def close(self):
            original.close()

    class Provider:
        def latest_accounts(self):
            con.execute("SELECT 1")
            return []

    monkeypatch.setattr(db, "reset_invalidated_instance", held_reset)
    con._inner = Poisoned()
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        cockpit_service=CockpitService(duckdb_path=path, provider_usage=Provider()),
    )
    server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    port = server.server_address[1]
    try:
        # The detecting request must escape section-warning isolation.
        status, body, headers = request(port, "/cockpit/overview")
        assert status == 503
        assert body["error"] == "analytical_store_unavailable"
        assert headers["Retry-After"] == "1"
        assert entered.wait(1)
        # No health query/open, even while recovery holds the connect lock.
        monkeypatch.setattr(
            collector,
            "render_readiness",
            lambda **kw: pytest.fail("health queried readiness"),
        )
        status, health, _ = request(port, "/healthz")
        assert status == 503
        assert health["process"] == "ok"
        assert health["analytical_store"]["status"] == "recovering"
        for route, method in [
            ("/cockpit/overview", "GET"),
            ("/analytics", "GET"),
            ("/insights", "GET"),
            ("/insights/content-excerpts", "DELETE"),
            ("/insights/content-analysis/consent", "POST"),
        ]:
            status, body, headers = request(port, route, method)
            assert status == 503
            assert body["error"] == "analytical_store_unavailable"
            assert headers["Retry-After"] == "1"
        dispatch = analytics_boundary_dispatcher(collector)
        assert dispatch("GET", "/cockpit/overview", "", b"").status == 503
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while db.analytical_store_health(path)["status"] == "recovering":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        try:
            assert request(port, "/healthz")[0] == 200

            # A subsequent request opens a new handle and works without a restart.
            def recovered_overview(filters):
                with db.open_duckdb_connection(path) as fresh:
                    return {"value": fresh.execute("SELECT 1").fetchone()[0]}

            collector.cockpit_service.overview = recovered_overview
            status, body, _ = request(port, "/cockpit/overview")
            assert status == 200
            assert body == {"value": 1}
        finally:
            server.shutdown()
            server.server_close()
    assert db.analytical_store_health(path)["status"] == "ok"
    with db.open_duckdb_connection(path) as fresh:
        assert fresh.execute("SELECT 1").fetchone() == (1,)


def test_readiness_cannot_cache_ok_during_recovery(tmp_path, monkeypatch):
    from drover.server.readiness import STATE_FAILED, STORE_ANALYTICAL, ReadinessProbe

    path = tmp_path / "analytics.duckdb"
    probe = ReadinessProbe(path)
    assert probe.check().ok
    monkeypatch.setitem(
        db._ANALYTICAL_STATES, db._path_key(path), db._AnalyticalState(status="failed")
    )
    report = probe.check()
    assert not report.ok
    assert any(
        s.store == STORE_ANALYTICAL and s.state == STATE_FAILED for s in report.stores
    )
