"""Regression coverage for hub work attribution at memory-pressure time."""

from __future__ import annotations

import logging

from drover.server import db
from drover.server.runtime_observability import (
    FootprintWatchdog,
    active_activities,
    activity,
)


def test_activity_logs_footprint_delta_and_exposes_active_work(caplog):
    readings = iter((100, 140))
    with caplog.at_level(logging.INFO, logger="drover.runtime_observability"):
        with activity(
            "native_usage_rollup",
            kind="background",
            footprint_reader=lambda: next(readings),
        ):
            assert active_activities() == [
                {"kind": "background", "name": "native_usage_rollup"}
            ]

    assert active_activities() == []
    assert (
        "hub_activity start kind=background name=native_usage_rollup footprint=100"
        in caplog.text
    )
    assert (
        "hub_activity end kind=background name=native_usage_rollup footprint=140 "
        "footprint_delta=40" in caplog.text
    )


def test_watchdog_dumps_work_and_stacks_once_per_threshold_crossing(caplog):
    readings = iter((100, 1025, 1026, 900, 1030))
    dumped = []
    watchdog = FootprintWatchdog(
        threshold_bytes=1024,
        footprint_reader=lambda: next(readings),
        stack_dumper=lambda: dumped.append("thread-1\n  frame"),
        activities=lambda: [{"kind": "request", "name": "GET /insights"}],
    )

    with caplog.at_level(logging.ERROR, logger="drover.runtime_observability"):
        for _ in range(5):
            watchdog.sample_once()

    assert dumped == ["thread-1\n  frame", "thread-1\n  frame"]
    assert caplog.text.count("hub_footprint_watchdog crossing") == 2
    assert caplog.text.count("GET /insights") == 2


def test_analytical_lock_logs_activity_holder_and_hold_time(tmp_path, caplog):
    path = tmp_path / "analytics.duckdb"
    generation = db.require_analytical_store(path)

    with caplog.at_level(logging.INFO, logger="drover.db"):
        with activity(
            "native_usage_rollup", kind="background", footprint_reader=lambda: 1
        ):
            with db._analytical_open_lock(path, generation):
                pass

    assert (
        "analytical_connect_lock acquire holder=background:native_usage_rollup"
        in caplog.text
    )
    assert (
        "analytical_connect_lock release holder=background:native_usage_rollup"
        in caplog.text
    )
