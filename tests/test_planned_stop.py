"""Planned-stop recovery stays bounded even when check-ins overlap."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from drover.server.planned_stop import PlannedStopStore, recover_planned_stop


def test_healthy_hub_clears_marker_without_restart(tmp_path):
    store = PlannedStopStore(tmp_path)
    store.record(step="point-release", actor="operator")
    restarts = []

    code, line = recover_planned_stop(
        store,
        ready_url="http://hub/readyz",
        log_path=tmp_path / "server.err.log",
        probe=lambda _: True,
        restart=lambda: restarts.append("restart"),
    )

    assert (code, line) == (0, "healthy")
    assert restarts == []
    assert not store.path.exists()


def test_valid_marker_restarts_once_and_reports_recovery(tmp_path):
    store = PlannedStopStore(tmp_path)
    store.record(step="lake-maintain", actor="operator")
    probes = iter([False, True])
    restarts = []

    code, line = recover_planned_stop(
        store,
        ready_url="http://hub/readyz",
        log_path=tmp_path / "server.err.log",
        probe=lambda _: next(probes),
        restart=lambda: restarts.append("restart"),
    )

    assert (code, line) == (0, "recovered")
    assert restarts == ["restart"]
    assert not store.path.exists()


def test_failed_recovery_reports_one_startup_error_and_never_restarts_twice(tmp_path):
    store = PlannedStopStore(tmp_path)
    store.record(step="lake-deprecate-legacy", actor="operator")
    log_path = tmp_path / "server.err.log"
    restarts = []

    def restart():
        restarts.append("restart")
        log_path.write_text(
            "ERROR lake_verification_required expected=old-pin actual=new-pin\n",
            encoding="utf-8",
        )

    code, line = recover_planned_stop(
        store,
        ready_url="http://hub/readyz",
        log_path=log_path,
        probe=lambda _: False,
        restart=restart,
        monotonic=iter([0.0, 31.0]).__next__,
    )

    assert code == 1
    assert (
        line
        == "failed: ERROR lake_verification_required expected=old-pin actual=new-pin"
    )
    assert len(line.splitlines()) == 1
    assert restarts == ["restart"]
    assert store.read()["attempts"] == 1

    code, line = recover_planned_stop(
        store,
        ready_url="http://hub/readyz",
        log_path=log_path,
        probe=lambda _: False,
        restart=lambda: restarts.append("second restart"),
    )
    assert (code, line) == (1, "unplanned outage: human decision required")
    assert restarts == ["restart"]


def test_missing_or_expired_marker_never_restarts(tmp_path):
    restarts = []
    store = PlannedStopStore(tmp_path)
    kwargs = {
        "ready_url": "http://hub/readyz",
        "log_path": tmp_path / "server.err.log",
        "probe": lambda _: False,
        "restart": lambda: restarts.append("restart"),
    }

    assert recover_planned_stop(store, **kwargs) == (
        1,
        "unplanned outage: human decision required",
    )
    store.record(
        step="cutover",
        actor="operator",
        now=datetime.now(timezone.utc) - timedelta(hours=3),
    )
    assert recover_planned_stop(store, **kwargs) == (
        1,
        "unplanned outage: human decision required",
    )
    assert restarts == []
