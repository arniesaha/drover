"""Contracts for the synthetic HTTP footprint measurement harness (#466)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from test_control_outbox import postgres_control_store
from test_lake_runtime import lake_spec
from test_lake_serving import verified_lake

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "measure_hub_memory.py"
SPEC = importlib.util.spec_from_file_location("measure_hub_memory", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
_growth_summary = MODULE._growth_summary
measure = MODULE.measure
prepare = MODULE.prepare


def test_harness_polls_cockpit_and_insights_routes_and_reports_growth(tmp_path):
    root = tmp_path / "fixture"
    prepare(root, sessions=40, spans=80, files=3)

    report = measure(root, minutes=0, interval_seconds=0)

    assert report["synthetic"] is True
    assert [
        request["path"].split("?", 1)[0] for request in report["endpoint_requests"]
    ] == [
        "/cockpit/overview",
        "/analytics",
        "/insights",
    ]
    assert all(request["status"] == 200 for request in report["endpoint_requests"])
    assert all(
        request["activity_status"] == "ok"
        for request in report["endpoint_requests"]
        if "activity_status" in request
    )
    assert report["samples"]
    assert all(sample["hub_memory"] > 0 for sample in report["samples"])
    assert report["peak_hub_memory"] <= 1024**3
    assert report["lifetime_peak_hub_memory"] >= report["peak_hub_memory"]
    assert [sample["path"] for sample in report["path_growth"]] == [
        "/cockpit/overview",
        "/analytics",
        "/insights",
    ]
    assert set(report["path_growth_summary"]) == {
        "/cockpit/overview",
        "/analytics",
        "/insights",
    }
    assert all(
        entry["hub_lifetime_after"] >= entry["hub_lifetime_before"]
        for entry in report["path_growth"]
    )
    assert report["python_growth_top"]


def test_growth_summary_keeps_per_route_brackets_separate():
    summary = _growth_summary(
        [
            {
                "path": "/analytics",
                "rss_before": 10,
                "rss_after": 18,
                "rss_growth_bytes": 8,
                "hub_before": 20,
                "hub_after": 25,
                "hub_growth_bytes": 5,
                "hub_lifetime_before": 20,
                "hub_lifetime_after": 25,
                "hub_lifetime_growth_bytes": 5,
            },
            {
                "path": "/insights",
                "rss_before": 18,
                "rss_after": 19,
                "rss_growth_bytes": 1,
                "hub_before": 25,
                "hub_after": 24,
                "hub_growth_bytes": -1,
                "hub_lifetime_before": 25,
                "hub_lifetime_after": 25,
                "hub_lifetime_growth_bytes": 0,
            },
            {
                "path": "/analytics",
                "rss_before": 19,
                "rss_after": 22,
                "rss_growth_bytes": 3,
                "hub_before": 24,
                "hub_after": 30,
                "hub_growth_bytes": 6,
                "hub_lifetime_before": 25,
                "hub_lifetime_after": 30,
                "hub_lifetime_growth_bytes": 5,
            },
        ]
    )

    assert summary == {
        "/analytics": {
            "calls": 2,
            "rss_net_growth_bytes": 12,
            "hub_net_growth_bytes": 10,
            "hub_lifetime_net_growth_bytes": 10,
            "rss_max_step_growth_bytes": 8,
            "hub_max_step_growth_bytes": 6,
            "hub_lifetime_max_step_growth_bytes": 5,
        },
        "/insights": {
            "calls": 1,
            "rss_net_growth_bytes": 1,
            "hub_net_growth_bytes": -1,
            "hub_lifetime_net_growth_bytes": 0,
            "rss_max_step_growth_bytes": 1,
            "hub_max_step_growth_bytes": -1,
            "hub_lifetime_max_step_growth_bytes": 0,
        },
    }


def test_linux_lifetime_peak_converts_ru_maxrss_kib_to_bytes(monkeypatch):
    """Linux ru_maxrss is KiB; it must not fall back to current RSS."""
    pid = 54321
    monkeypatch.setattr(MODULE, "platform", "linux")
    monkeypatch.setattr(MODULE.os, "getpid", lambda: pid)
    monkeypatch.setattr(
        MODULE,
        "resource",
        SimpleNamespace(
            RUSAGE_SELF=object(),
            getrusage=lambda _who: SimpleNamespace(ru_maxrss=12_345),
        ),
        raising=False,
    )
    monkeypatch.setattr(MODULE, "hub_memory_bytes", lambda _pid: 1)

    assert MODULE._hub_lifetime_peak(pid) == 12_345 * 1024


def test_selected_ducklake_http_routes_use_bounded_disposable_children(
    verified_lake, monkeypatch, tmp_path
):
    """The v2 fixture proves that the production routes do not open a lake here.

    The imported fixture creates a private PostgreSQL catalog and pinned
    extensions. It skips when those deliberately opt-in local artifacts are
    unavailable; it never falls back to a developer's configured hub.
    """
    from drover.server.advisory.service import InsightsService
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
        # Keep the config/control fixture private; do not let the lazily
        # constructed insight service discover a developer's hub config.
        advisory_service=InsightsService(
            control_path, config_path=tmp_path / "config.toml"
        ),
    )
    server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    rss_samples = [process_rss(os.getpid())]
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for path in (
            "/cockpit/overview?days=30",
            "/analytics?days=29",
            "/insights",
        ):
            with urllib.request.urlopen(base + path, timeout=15) as response:
                payload = json.loads(response.read())
            assert response.status == 200
            if "activity" in payload:
                assert payload["activity"]["status"] == "ok", payload["activity"]
            rss_samples.append(process_rss(os.getpid()))
    finally:
        server.shutdown()
        server.server_close()

    assert child_peaks
    assert max(child_peaks) <= QueryLimits().rss_bytes
    assert all(sample > 0 for sample in rss_samples)
