"""Hub RSS budgets are process measurements, independent of engine/child budgets."""

import json
import os

import pytest

from drover.config import MemoryBudgetConfig, default_config, load_config
from drover.server import process_memory
from drover.server.process_memory import ProcessMemoryGuard
from drover.server.readiness import ReadinessReport, StoreProbe


def test_server_budget_defaults_and_config_validation(tmp_path):
    assert default_config().memory.rss_budget_bytes == 4 * 1024**3
    config = tmp_path / "config.toml"
    config.write_text(
        "[memory]\nrss_budget_bytes=2048\nsample_interval_seconds=0.1\nwarn_fraction=0.75\n"
    )
    assert load_config(config).memory == MemoryBudgetConfig(2048, 0.1, 0.75)
    for value in (-1, True, 1.5):
        with pytest.raises(ValueError):
            MemoryBudgetConfig(rss_budget_bytes=value)
    for value in (0, float("nan")):
        with pytest.raises(ValueError):
            MemoryBudgetConfig(sample_interval_seconds=value)
    for value in (0, 1, float("inf")):
        with pytest.raises(ValueError):
            MemoryBudgetConfig(warn_fraction=value)


def test_states_and_warning_over_budget_exclude_children(caplog):
    current = [10]
    seen = []

    def reader(pid):
        seen.append(pid)
        return current[0]

    guard = ProcessMemoryGuard(MemoryBudgetConfig(100, 1, 0.8), reader=reader)
    guard.child_started(999999)
    guard.child_sample(2 * 1024**3)
    guard.sample()
    assert guard.snapshot()["state"] == "ok"
    current[0] = 80
    guard.sample()
    assert guard.snapshot()["state"] == "warn"
    current[0] = 101
    guard.sample()
    payload = guard.snapshot()
    assert payload["state"] == "over"
    assert payload["rss_bytes"] == 101
    assert payload["query_children"]["active"] == 1
    assert payload["query_children"]["peak_rss_bytes"] == 2 * 1024**3
    assert set(seen) == {os.getpid()}
    assert "RSS over budget" in caplog.text
    guard.child_finished(999999)
    assert guard.snapshot()["query_children"]["completed"] == 1
    assert guard.snapshot()["query_children"]["active"] == 0


def test_readiness_exposes_live_memory_even_with_cached_store_report(monkeypatch):
    guard = ProcessMemoryGuard(MemoryBudgetConfig(100), reader=lambda pid: 101)
    monkeypatch.setattr(process_memory, "_guard", guard)
    report = ReadinessReport(
        (StoreProbe("control_plane", "ok"),), 0, memory={"jobs": {}, "available": True}
    )
    for detailed in (False, True):
        code, body = report.as_response(include_detail=detailed)
        payload = json.loads(body)
        assert code == 200
        assert payload["memory"]["budget_bytes"] == 100
        assert payload["memory"]["rss_bytes"] == 101
        assert payload["memory"]["state"] == "over"
        assert "query_children" in payload["memory"]


def test_data_quality_has_the_same_hub_memory_fields(monkeypatch, tmp_path):
    from drover.server import quality
    from drover.server.mcp.tools import drover_data_quality

    guard = ProcessMemoryGuard(reader=lambda pid: 12345)
    monkeypatch.setattr(process_memory, "_guard", guard)
    monkeypatch.setattr(quality, "runtime_audit", lambda **kwargs: {})
    payload = drover_data_quality(duckdb_path=tmp_path / "unused.duckdb")
    assert payload["memory"] == guard.snapshot()


def test_guard_samples_periodically_and_stops():
    import threading

    sampled = threading.Event()
    count = [0]

    def reader(pid):
        count[0] += 1
        if count[0] >= 2:
            sampled.set()
        return 100

    guard = ProcessMemoryGuard(
        MemoryBudgetConfig(sample_interval_seconds=0.01), reader=reader
    )
    guard.start()
    try:
        assert sampled.wait(1)
    finally:
        guard.stop()
    assert not guard._thread.is_alive()


def test_native_process_rss_and_missing_pid():
    assert process_memory.process_rss(os.getpid()) > 0
    with pytest.raises((OSError, ValueError)):
        process_memory.process_rss(2147483647)


def test_eight_gib_hub_reports_over_default_four_gib_budget(caplog):
    """The reported production-size excess must be visible before cutover."""
    guard = ProcessMemoryGuard(reader=lambda pid: 8 * 1024**3)
    guard.sample()
    payload = guard.snapshot()
    assert payload["state"] == "over"
    assert payload["rss_bytes"] == 8589934592
    assert payload["budget_bytes"] == 4294967296
    assert payload["peak_rss_bytes"] == 8589934592
    assert "rss_bytes=8589934592 budget_bytes=4294967296" in caplog.text
