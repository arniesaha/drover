"""Foreground-priority coordination for analytical maintenance workers."""

from __future__ import annotations

from pathlib import Path


def test_foreground_waiter_blocks_new_maintenance_admission():
    """A request admitted first prevents a new historical worker pass from starting."""
    from drover.server.analytics_maintenance import AnalyticalMaintenanceGate

    gate = AnalyticalMaintenanceGate()

    with gate.foreground():
        assert gate.try_begin_maintenance() is False

    assert gate.try_begin_maintenance() is True
    gate.end_maintenance()


def test_native_usage_rollup_skips_without_opening_store_when_maintenance_is_active():
    """A held span-maintenance slot makes native usage return without a DB read."""
    from drover.server.analytics_maintenance import AnalyticalMaintenanceGate
    from drover.server.native_usage_rollup import rollup_pending_native_usage

    gate = AnalyticalMaintenanceGate()
    assert gate.try_begin_maintenance() is True
    try:
        report = rollup_pending_native_usage(
            Path("does-not-need-to-exist.duckdb"), maintenance_gate=gate
        )
    finally:
        gate.end_maintenance()

    assert (report.partitions, report.sessions) == (0, 0)


def test_span_rollup_skips_without_opening_store_when_maintenance_is_active():
    """A held native-maintenance slot makes span projection return before discovery."""
    from drover.server.analytics_maintenance import AnalyticalMaintenanceGate
    from drover.server.span_analytics_rollup import rollup_pending_span_analytics

    gate = AnalyticalMaintenanceGate()
    assert gate.try_begin_maintenance() is True
    try:
        report = rollup_pending_span_analytics(
            Path("does-not-need-to-exist.duckdb"), maintenance_gate=gate
        )
    finally:
        gate.end_maintenance()

    assert (report.partitions, report.sessions, report.pending) == (0, 0, 0)
