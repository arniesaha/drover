"""Synthetic collector progress, doctor output and freshness contracts."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from drover.collect.health import (
    format_collector_health,
    read_collector_health,
    stage_health,
)
from drover.collect.shipper import ship_staging
from drover.server.quality import _freshness_category


@pytest.mark.parametrize(
    "age,successful,caught_up,expected",
    [
        (20, True, True, "ok"),
        (601, True, True, "warn"),
        (3601, True, True, "critical"),
        (20, False, True, "warn"),
        (20, True, False, "warn"),
        (None, False, True, "unknown"),
    ],
)
def test_collector_health_is_distinct_from_event_age(
    tmp_path, age, successful, caught_up, expected
):
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    incoming = tmp_path / "incoming"
    stamp = (now - timedelta(seconds=age)).isoformat() if age is not None else None
    stage_health(
        incoming / "fixture-host",
        "openclaw_sqlite",
        {
            "last_successful_collect_at": stamp,
            "last_run_iso": now.isoformat(),
            "collect_successful": successful,
            "caught_up": caught_up,
            "agents": {"private": "never shipped"},
        },
    )
    rows = read_collector_health(incoming, now=now)
    assert len(rows) == 1
    assert rows[0]["collector_lag_seconds"] == age
    assert rows[0]["last_successful_collect_at"] == stamp
    assert rows[0]["status"] == expected
    text = format_collector_health(rows)
    assert "openclaw_sqlite" in text
    assert "last successful collect=" in text
    assert "collector lag=" in text
    assert (
        "private"
        not in (
            incoming / "fixture-host" / ".collector-openclaw_sqlite.json"
        ).read_text()
    )
    category = _freshness_category(
        {
            "latest_events": {"openclaw": {"timestamp": now.isoformat()}},
            "span_integration": "disabled",
            "collectors": rows,
        },
        now=now,
    )
    assert category["status"] == expected
    assert category["details"]["collectors"] == rows


@pytest.mark.parametrize(
    "data",
    ["broken-json", "[]", '{"last_successful_collect_at": "invalid"}', "x" * 4097],
)
def test_invalid_collector_metadata_is_unknown(tmp_path, data):
    host = tmp_path / "fixture-host"
    host.mkdir()
    (host / ".collector-openclaw_sqlite.json").write_text(data)
    rows = read_collector_health(tmp_path)
    assert rows[0]["status"] == "unknown"
    assert rows[0]["collector_lag_seconds"] is None


def test_missing_collector_metadata_is_explicitly_unknown(tmp_path):
    assert "unknown" in format_collector_health(read_collector_health(tmp_path))


def test_shipper_ships_health_on_idle_runs_and_ignores_temporary_files(tmp_path):
    stage_health(tmp_path, "openclaw_sqlite", {"last_successful_collect_at": None})
    (tmp_path / ".collector-openclaw_sqlite.json.tmp").write_text("unfinished")
    captured = []

    class Result:
        returncode = 0

    result = ship_staging(
        staging_dir=tmp_path,
        host="fixture-hub",
        host_id="fixture-host",
        _runner=lambda cmd: captured.extend(cmd) or Result(),
    )
    assert result.files == 1
    assert str(tmp_path / ".collector-openclaw_sqlite.json") in captured
    assert not any(arg.endswith(".tmp") for arg in captured)


@pytest.mark.parametrize("backend", ["legacy", "ducklake"])
def test_doctor_command_reports_collector_lag_for_both_backends(
    tmp_path, monkeypatch, backend
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from click.testing import CliRunner

    from drover.server import __main__ as cli

    incoming = tmp_path / "incoming"
    stamp = datetime.now(timezone.utc).isoformat()
    stage_health(
        incoming / "fixture-host",
        "openclaw_sqlite",
        {
            "last_successful_collect_at": stamp,
            "collect_successful": True,
            "caught_up": True,
        },
    )
    cfg = SimpleNamespace(
        analytics=SimpleNamespace(backend=backend),
        incoming_dir=incoming,
        duckdb_path=tmp_path / "test.duckdb",
        parquet_dir=tmp_path / "parquet",
        spans_enabled=False,
    )
    monkeypatch.setattr(cli, "_resolve_config", lambda _: cfg)
    monkeypatch.setattr(
        cli,
        "_local_api_request",
        lambda *_, **kw: {"exporter": {"running": True, "state": "ok"}},
    )
    monkeypatch.setattr(cli, "_bootstrap_if_missing", lambda _: None)
    monkeypatch.setattr(cli, "_diagnostic_db_path", lambda path: nullcontext(path))
    monkeypatch.setattr(
        cli,
        "audit_lakehouse",
        lambda **_: {
            "agent_events_total": 0,
            "spans_total": None,
            "sessions_total": 0,
            "tasks_total": 0,
            "summaries_total": 0,
            "agent_events_by_partition": {},
            "processed_files": {},
            "warnings": [],
        },
    )
    result = CliRunner().invoke(cli.main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "fixture-host/openclaw_sqlite: ok" in result.output
    assert f"last successful collect={stamp}" in result.output
    assert "collector lag=" in result.output


def test_runtime_audit_includes_received_collector_metadata(tmp_path):
    from drover.schema import bootstrap
    from drover.server.doctor import format_runtime_audit, runtime_audit

    incoming = tmp_path / "incoming"
    parquet = tmp_path / "parquet"
    db = tmp_path / "test.duckdb"
    bootstrap(parquet_dir=parquet, duckdb_path=db)
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    stamp = (now - timedelta(seconds=1200)).isoformat()
    stage_health(
        incoming / "fixture-host",
        "openclaw_sqlite",
        {"last_successful_collect_at": stamp},
    )
    audit = runtime_audit(duckdb_path=db, incoming_dir=incoming, now=now, deep=False)
    assert audit["collectors"][0]["collector_lag_seconds"] == 1200
    assert "collector lag=1200.0s" in format_runtime_audit(audit)
