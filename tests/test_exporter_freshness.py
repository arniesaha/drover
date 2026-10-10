"""Exporter readiness, supervised recovery and doctor share one verdict."""

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from drover.server.doctor import format_exporter_health
from drover.server.lake import lifecycle
from drover.server.lake.freshness import freshness_status, read_freshness
from drover.server.lake.runtime import LakeError


@pytest.mark.parametrize(
    "running,age,state",
    [
        (True, 0, "ok"),
        (True, 45, "lagging"),
        (True, 352, "stalled"),
        (False, 0, "stopped"),
    ],
)
def test_verdict_and_doctor(running, age, state):
    now = datetime.now(timezone.utc)
    snapshot = {
        "unacknowledged_batches": int(age > 0),
        "oldest_unacknowledged_at": (
            (now - timedelta(seconds=age)).isoformat() if age else None
        ),
    }
    health = freshness_status(snapshot, running=running, now=now)
    assert health["state"] == state
    assert health["oldest_unacknowledged_age_seconds"] == age
    text = format_exporter_health(health)
    assert f"exporter: {state}" in text
    assert health["recovery_action"] in text


def test_pending_rows_are_not_silently_fresh():
    now = datetime.now(timezone.utc)
    health = freshness_status(
        {"oldest_pending_at": (now - timedelta(seconds=352)).isoformat()},
        running=True,
        now=now,
    )
    assert health["state"] == "stalled"
    assert health["oldest_unacknowledged_age_seconds"] == 0


def test_control_freshness_idle_and_unacked(pg_control_path):
    from drover.server.db import control_plane_connection

    assert read_freshness(pg_control_path)["unacknowledged_batches"] == 0
    now = datetime.now(timezone.utc)
    with control_plane_connection(pg_control_path) as con:
        con.execute(
            "INSERT INTO control_outbox_batches (batch_id,state,member_count,created_at) VALUES ('freshness','claimed',1,?)",
            [now - timedelta(seconds=352)],
        )
    health = freshness_status(read_freshness(pg_control_path), running=True, now=now)
    assert health["unacknowledged_batches"] == 1
    assert health["oldest_unacknowledged_age_seconds"] == 352
    assert health["state"] == "stalled"


def test_supervisor_cap_and_backoff(monkeypatch):
    worker = lifecycle.ExporterLifecycle(SimpleNamespace())
    worker._started = True
    waits = []

    class Stop:
        def is_set(self):
            return False

        def wait(self, delay):
            waits.append(delay)
            return False

    worker._stop = Stop()
    attempts = []

    def die(shutdown):
        attempts.append(1)
        worker._running = False
        worker._error = "lake_export_deadline"

    monkeypatch.setattr(worker, "_run", die)
    worker._supervise(threading.Event())
    assert len(attempts) == 6
    assert waits == [1, 2, 4, 8, 16]
    assert worker.health()["state"] == "stopped"
    assert worker.health()["restart_count"] == 5


def test_readyz_exporter_status_is_separate(monkeypatch, tmp_path):
    from drover.server.metrics import MetricsCollector
    from drover.server.readiness import ReadinessProbe

    class Result:
        def as_response(self, **kwargs):
            return 200, json.dumps({"stores": []})

    monkeypatch.setattr(ReadinessProbe, "check", lambda self: Result())
    health = freshness_status({}, running=False, last_error="lake_export_deadline")
    collector = MetricsCollector(
        duckdb_path=tmp_path / "db",
        incoming_dir=tmp_path,
        summarizer_report={},
        exporter_health=lambda: health,
    )
    status, body = collector.render_readiness(include_detail=False)
    assert status == 200
    assert json.loads(body)["exporter"] == health


@pytest.mark.parametrize("state", ["ok", "lagging", "stalled", "stopped"])
def test_doctor_command_uses_live_verdict(monkeypatch, state):
    from click.testing import CliRunner

    from drover.server import __main__ as cli

    monkeypatch.setattr(
        cli,
        "_resolve_config",
        lambda _: SimpleNamespace(analytics=SimpleNamespace(backend="ducklake")),
    )
    health = freshness_status({}, running=state != "stopped")
    health["state"] = state
    calls = []

    def request(cfg, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"exporter": health}

    monkeypatch.setattr(cli, "_local_api_request", request)
    result = CliRunner().invoke(cli.main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert f"exporter: {state}" in result.output
    assert calls == [("GET", "/readyz", {"allow_unready": True})]


def test_readyz_forwards_split_worker_snapshot(monkeypatch, tmp_path):
    from drover.server.metrics import MetricsCollector
    from drover.server.readiness import ReadinessProbe

    class Result:
        def as_response(self, **kwargs):
            return 200, json.dumps({"stores": []})

    monkeypatch.setattr(ReadinessProbe, "check", lambda self: Result())
    health = freshness_status({}, running=False, last_error="lake_export_deadline")
    collector = MetricsCollector(
        duckdb_path=tmp_path / "db",
        incoming_dir=tmp_path,
        summarizer_report={},
        exporter_health=lambda: {"state": "degraded", "exporter": health},
    )
    status, body = collector.render_readiness()
    payload = json.loads(body)
    assert payload["exporter"] == health
    assert payload["stores"][-1]["state"] == "degraded"
    assert status == 200


def test_shutdown_during_backoff_prevents_replacement(monkeypatch):
    worker = lifecycle.ExporterLifecycle(SimpleNamespace())
    worker._started = True
    attempts = []

    class Stop:
        def is_set(self):
            return False

        def wait(self, delay):
            return True

    worker._stop = Stop()
    monkeypatch.setattr(worker, "_run", lambda shutdown: attempts.append(1))
    worker._supervise(threading.Event())
    assert len(attempts) == 1
    assert not worker.health()["recovery_pending"]


@pytest.mark.parametrize("deadline", [0, -1, float("nan"), float("inf"), True, "120"])
def test_exporter_recovery_deadline_rejects_invalid_config(deadline):
    from drover.config import AnalyticsConfig

    with pytest.raises(ValueError, match="exporter_recovery_deadline_seconds"):
        AnalyticsConfig(exporter_recovery_deadline_seconds=deadline)


def test_repeated_live_hangs_exhaust_shared_restart_budget(monkeypatch, tmp_path):
    import time

    from drover.server.lake import task_projection

    monkeypatch.setattr(lifecycle, "FRESHNESS_POLL_SECONDS", 0.005)
    monkeypatch.setattr(lifecycle, "RESTART_BACKOFF_SECONDS", 0.02)
    monkeypatch.setattr(lifecycle, "MAX_RESTARTS", 2)
    monkeypatch.setattr(lifecycle, "lake_spec", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "_check_export_catalog", lambda *a: None)
    monkeypatch.setattr(lifecycle, "read_freshness", lambda *a: {})
    monkeypatch.setattr(lifecycle.ExporterLifecycle, "_checkpoint", lambda *a: None)
    monkeypatch.setattr(task_projection, "refresh_if_provisioned", lambda *a, **k: None)
    starts, exits = [], []

    class HangingExporter:
        def __init__(self, **kwargs):
            self.event = threading.Event()

        def __enter__(self):
            assert len(starts) == len(exits), "overlapping owners"
            starts.append(time.monotonic())
            return self

        def __exit__(self, *args):
            exits.append(time.monotonic())

        def run_once(self):
            assert self.event.wait(3), "live batch never cancelled"
            raise LakeError("lake_export_recovery_deadline")

        def cancel(self):
            self.event.set()

    monkeypatch.setattr(lifecycle, "LakeOutboxExporter", HangingExporter)
    worker = lifecycle.ExporterLifecycle(
        SimpleNamespace(
            duckdb_path=tmp_path,
            analytics=SimpleNamespace(
                retire_legacy_writers=False, exporter_recovery_deadline_seconds=0.03
            ),
        )
    )
    try:
        worker.start(shutdown_event=threading.Event())
        worker._thread.join(2)
        assert not worker._thread.is_alive()
        assert len(starts) == len(exits) == 3
        assert starts[1] - exits[0] >= 0.02
        assert starts[2] - exits[1] >= 0.04
        health = worker.health()
        assert health["state"] == "stopped"
        assert health["restart_count"] == 2
        assert health["last_error"] == "lake_export_recovery_deadline"
    finally:
        worker.stop()


def test_unresponsive_cancellation_stops_without_replacement(monkeypatch):
    worker = lifecycle.ExporterLifecycle(SimpleNamespace())
    worker._running = True
    worker._owner = SimpleNamespace(cancel=lambda: None)
    worker._batch_started_at = 0
    worker._batch_deadline = 1
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: 10)
    worker._watch_batch()
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: 46)
    worker._watch_batch()
    assert worker._stop.is_set()
    assert worker.health()["state"] == "stopped"
    assert worker.health()["last_error"] == "lake_export_cancellation_stuck"
    assert worker.health()["restart_count"] == 0


def test_projection_refresh_does_not_inherit_batch_cancellation(monkeypatch, tmp_path):
    from drover.server.lake import task_projection

    monkeypatch.setattr(lifecycle, "FRESHNESS_POLL_SECONDS", 0.005)
    monkeypatch.setattr(lifecycle, "lake_spec", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "_check_export_catalog", lambda *a: None)
    monkeypatch.setattr(lifecycle, "read_freshness", lambda *a: {})
    monkeypatch.setattr(lifecycle.ExporterLifecycle, "_checkpoint", lambda *a: None)
    projecting, release, cancelled = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    refreshes = []

    def refresh(*args, **kwargs):
        refreshes.append(1)
        if len(refreshes) == 2:
            projecting.set()
            assert release.wait(3)

    class Exporter:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run_once(self):
            return {"acknowledged": 1}

        def cancel(self):
            cancelled.set()

    monkeypatch.setattr(lifecycle, "LakeOutboxExporter", Exporter)
    monkeypatch.setattr(task_projection, "refresh_if_provisioned", refresh)
    worker = lifecycle.ExporterLifecycle(
        SimpleNamespace(
            duckdb_path=tmp_path,
            analytics=SimpleNamespace(
                retire_legacy_writers=False, exporter_recovery_deadline_seconds=0.03
            ),
        )
    )
    try:
        worker.start(shutdown_event=threading.Event())
        assert projecting.wait(1)
        cancelled.wait(0.1)
        assert not worker.health()["recovery_pending"]
        assert worker.health()["restart_count"] == 0
    finally:
        release.set()
        worker.stop()


def test_completed_batch_disarms_watchdog_atomically(monkeypatch):
    """Watchdog interleaving after completion must not cancel follow-up work."""
    worker = lifecycle.ExporterLifecycle(SimpleNamespace())
    now = [0]
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: now[0])
    worker._batch_deadline = 1
    worker._owner = SimpleNamespace(
        run_once=lambda: {"acknowledged": 1}, cancel=lambda: None
    )

    class InterleavingLock:
        def __init__(self):
            self.entries = 0

        def __enter__(self):
            self.entries += 1

        def __exit__(self, *args):
            if self.entries == 2:
                # Watchdog acquires just after the successful return path's
                # critical section, before its finally cleanup can reacquire.
                now[0] = 2
                worker._watch_batch()

    worker._owner_lock = InterleavingLock()
    assert worker._export_batch(worker._owner) == {"acknowledged": 1}
    assert not worker.health()["recovery_pending"]


def test_blocking_cancel_request_does_not_block_watchdog(monkeypatch):
    worker = lifecycle.ExporterLifecycle(SimpleNamespace())
    entered, release, watched = threading.Event(), threading.Event(), threading.Event()

    def blocking_cancel():
        entered.set()
        release.wait(3)

    worker._owner = SimpleNamespace(cancel=blocking_cancel)
    worker._batch_started_at = 0
    worker._batch_deadline = 1
    worker._running = True
    now = [10]
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: now[0])

    def watch():
        try:
            worker._watch_batch()
        finally:
            watched.set()

    task = threading.Thread(target=watch)
    task.start()
    try:
        assert entered.wait(1)
        assert watched.wait(0.1), "cancel request blocked watchdog"
        # Owner may be in cleanup waiting for the cancellation lock, after
        # batch timer was disarmed. Grace still must fail closed in this case.
        worker._batch_started_at = None
        now[0] = 46
        worker._watch_batch()
        assert worker.health()["state"] == "stopped"
        assert worker.health()["last_error"] == "lake_export_cancellation_stuck"
        assert worker.health()["restart_count"] == 0
    finally:
        release.set()
        task.join(3)
