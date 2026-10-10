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
