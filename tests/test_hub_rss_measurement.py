"""Contracts for the synthetic HTTP RSS measurement harness (#466)."""

from __future__ import annotations

import json
import os
import urllib.request

from test_control_outbox import postgres_control_store
from test_lake_runtime import lake_spec
from test_lake_serving import verified_lake

from scripts.measure_hub_memory import measure, prepare


def test_harness_polls_both_cockpit_http_routes_and_reports_growth(tmp_path):
    root = tmp_path / "fixture"
    prepare(root, sessions=40, spans=80, files=3)

    report = measure(root, minutes=0, interval_seconds=0)

    assert report["synthetic"] is True
    assert [
        request["path"].split("?", 1)[0] for request in report["endpoint_requests"]
    ] == [
        "/cockpit/overview",
        "/analytics",
    ]
    assert all(request["status"] == 200 for request in report["endpoint_requests"])
    assert all(
        request["activity_status"] == "ok" for request in report["endpoint_requests"]
    )
    assert report["samples"]
    assert all(sample["hub_memory"] > 0 for sample in report["samples"])
    assert report["python_growth_top"]


def test_selected_ducklake_http_routes_use_bounded_disposable_children(
    verified_lake, monkeypatch, tmp_path
):
    """The v2 fixture proves that the production routes do not open a lake here.

    The imported fixture creates a private PostgreSQL catalog and pinned
    extensions. It skips when those deliberately opt-in local artifacts are
    unavailable; it never falls back to a developer's configured hub.
    """
    from drover.server.cockpit.service import CockpitService
    from drover.server.lake import read_models
    from drover.server.lake.query_process import QueryLimits
    from drover.server.lake.serving import configure_analytics
    from drover.server.metrics import MetricsCollector
    from drover.server.process_memory import process_rss
    from drover.server.web.app import start_metrics_server

    _, control_path, config = verified_lake
    configure_analytics(control_path, config)
    child_peaks = []
    query = read_models.query

    def observed_query(*args, **kwargs):
        result = query(*args, **kwargs)
        child_peaks.append(result["peak_rss_bytes"])
        return result

    monkeypatch.setattr(read_models, "query", observed_query)
    collector = MetricsCollector(
        duckdb_path=control_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        cockpit_service=CockpitService(
            duckdb_path=control_path, provider_usage=None, cursor_secret=b"x" * 32
        ),
    )
    server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    rss_samples = [process_rss(os.getpid())]
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for path in ("/cockpit/overview?days=30", "/analytics?days=29"):
            with urllib.request.urlopen(base + path, timeout=15) as response:
                payload = json.loads(response.read())
            assert response.status == 200
            assert payload["activity"]["status"] == "ok", payload["activity"]
            rss_samples.append(process_rss(os.getpid()))
    finally:
        server.shutdown()
        server.server_close()

    assert child_peaks
    assert max(child_peaks) <= QueryLimits().rss_bytes
    assert all(sample > 0 for sample in rss_samples)
