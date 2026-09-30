"""Foreground-priority admission for background analytical work (#331)."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from drover.server.analytics_maintenance import (
    AnalyticalMaintenanceGate,
    MaintenanceAdmission,
    latest_maintenance_gate_stats,
)


def test_a_pass_stands_aside_while_a_request_is_in_flight() -> None:
    gate = AnalyticalMaintenanceGate()
    admission = MaintenanceAdmission(gate)

    with gate.foreground():
        with admission.admit() as admitted:
            assert admitted is False

    with admission.admit() as admitted:
        assert admitted is True


def test_long_build_never_forces_competing_maintenance() -> None:
    gate = AnalyticalMaintenanceGate()
    admission = MaintenanceAdmission(gate)

    with gate.foreground():
        for _ in range(25):
            with admission.admit() as admitted:
                assert admitted is False
        assert not gate.stats().maintenance_active

    assert admission.skipped_total == 25
    with admission.admit() as admitted:
        assert admitted is True


def test_two_passes_never_hold_the_slot_at_once() -> None:
    gate = AnalyticalMaintenanceGate()
    first = MaintenanceAdmission(gate)
    second = MaintenanceAdmission(gate)

    with first.admit() as first_admitted:
        with second.admit() as second_admitted:
            assert first_admitted is True
            assert second_admitted is False


def test_the_slot_is_released_when_a_pass_raises() -> None:
    gate = AnalyticalMaintenanceGate()
    admission = MaintenanceAdmission(gate)

    with pytest.raises(RuntimeError):
        with admission.admit() as admitted:
            assert admitted is True
            raise RuntimeError("pass blew up")

    assert gate.try_begin_maintenance() is True
    gate.end_maintenance()


def test_foreground_count_survives_concurrent_requests() -> None:
    gate = AnalyticalMaintenanceGate()
    started = threading.Barrier(3)
    release = threading.Event()

    def request() -> None:
        with gate.foreground():
            started.wait(timeout=5)
            release.wait(timeout=5)

    threads = [threading.Thread(target=request) for _ in range(2)]
    for thread in threads:
        thread.start()
    started.wait(timeout=5)
    try:
        assert gate.stats().foreground_waiters == 2
        assert latest_maintenance_gate_stats().foreground_waiters == 2
        assert gate.try_begin_maintenance() is False
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)

    assert gate.stats().foreground_waiters == 0
    assert gate.try_begin_maintenance() is True
    gate.end_maintenance()


def test_no_gate_means_every_pass_runs() -> None:
    """Tests and CLI entry points build workers without a gate."""
    admission = MaintenanceAdmission(None)
    for _ in range(3):
        with admission.admit() as admitted:
            assert admitted is True
    assert admission.skipped_total == 0


def test_the_advisory_sweep_defers_to_a_request(tmp_path) -> None:
    """Wiring check: the worker consults the gate, not just the helper."""
    from drover.schema import bootstrap
    from drover.server.advisory.repository import AdvisoryRepository
    from drover.server.advisory.worker import AdvisoryWorker

    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    gate = AnalyticalMaintenanceGate()

    def explode(*args, **kwargs):
        raise AssertionError("the sweep must not touch the store while deferred")

    worker = AdvisoryWorker(
        duckdb_path=db,
        repository=AdvisoryRepository(db),
        snapshot_factory=explode,
        maintenance_gate=gate,
    )

    scheduler = SimpleNamespace(enqueue_due_full_review=explode)
    with gate.foreground():
        for _ in range(25):
            result = worker.run_once([], scheduler=scheduler)

    assert (result.succeeded, result.failed, result.skipped) == (0, 0, 0)
    assert worker.deferred_sweeps == 25


def test_the_native_rollup_defers_to_a_request(tmp_path, monkeypatch) -> None:
    from drover.server import native_usage_rollup
    from drover.server.native_usage_rollup import NativeUsageRollupWorker

    gate = AnalyticalMaintenanceGate()
    monkeypatch.setattr(
        native_usage_rollup,
        "rollup_pending_native_usage",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the rollup must not scan while deferred")
        ),
    )
    worker = NativeUsageRollupWorker(
        duckdb_path=tmp_path / "drover.duckdb", maintenance_gate=gate
    )

    with gate.foreground():
        for _ in range(25):
            report = worker.drain_once()

    assert (report.partitions, report.sessions) == (0, 0)
    assert worker.deferred_passes == 25


@pytest.mark.parametrize("skipped_ticks", [1, 3])
def test_day_summaries_retry_promptly_after_deferral(
    tmp_path, monkeypatch, skipped_ticks
):
    from drover import schema
    from drover.server.__main__ import _backfill_day_summaries

    gate = AnalyticalMaintenanceGate()
    foreground = gate.foreground()
    foreground.__enter__()
    waits = []
    scans = []

    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            waits.append(seconds)
            if len(waits) == skipped_ticks:
                foreground.__exit__(None, None, None)
            elif len(waits) > skipped_ticks:
                self.stopped = True

    def backfill(path):
        assert not gate.stats().foreground_waiters
        assert gate.stats().maintenance_active
        scans.append(path)
        return 1

    monkeypatch.setattr(schema, "backfill_agent_event_day_summary", backfill)
    path = tmp_path / "analytics.duckdb"
    _backfill_day_summaries(path, gate, Stop())
    assert waits == [30.0] * skipped_ticks + [900.0]
    assert scans == [path]
    assert not gate.stats().maintenance_active
