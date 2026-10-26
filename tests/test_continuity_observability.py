from datetime import datetime, timezone

from drover.schema import bootstrap_control_plane_store
from drover.server.continuity_hub import ContinuityHub
from drover.server.mcp.tools import drover_continuity_status


def test_status_empty_store(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)

    status = ContinuityHub(path).observability_status(
        now=datetime(2026, 10, 26, tzinfo=timezone.utc)
    )

    assert status == {
        "version": 1,
        "pending_wakes": {
            "total": 0,
            "under_2m": 0,
            "between_2m_and_10m": 0,
            "over_10m": 0,
        },
        "latencies": {
            "sample_count": 0,
            "wake_to_consume_ms": {"avg": 0, "p50": 0, "p95": 0},
            "consume_to_ack_ms": {"avg": 0, "p50": 0, "p95": 0},
        },
        "active_leases": [],
        "idle_undeclared_sessions": 0,
        "redelivered_wakes_count": 0,
        "plugin_heartbeats": [],
        "active_alerts": [],
    }


def test_status_latency_percentiles(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    hub = ContinuityHub(path)
    for latency in (100, 200, 300, 400, 500):
        hub.status_service.record_transition("wake_to_consume", latency)
    hub.status_service.record_transition("consume_to_ack", 900)

    status = hub.observability_status(now=datetime(2026, 10, 26, tzinfo=timezone.utc))

    assert status["latencies"] == {
        "sample_count": 6,
        "wake_to_consume_ms": {"avg": 300, "p50": 300, "p95": 500},
        "consume_to_ack_ms": {"avg": 900, "p50": 900, "p95": 900},
    }


def test_mcp_status_read_matches_hub_projection(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)

    assert drover_continuity_status(duckdb_path=path)["pending_wakes"]["total"] == 0
