"""Scheduling policy for bounded, low-priority DuckLake compaction."""

from datetime import UTC, datetime, timedelta

import pytest


def test_compaction_pressure_bypasses_low_priority_window():
    from drover.server.lake.maintenance import CompactionPolicy, compaction_due

    policy = CompactionPolicy(soft_file_limit=5_000, daily_window_hour=3)
    now = datetime(2026, 10, 20, 3, 15, tzinfo=UTC)

    assert compaction_due(
        policy,
        referenced_files=5_001,
        last_completed_at=now,
        now=now,
    )
    assert compaction_due(
        policy,
        referenced_files=12,
        last_completed_at=now - timedelta(days=1),
        now=now,
    )
    assert compaction_due(
        policy,
        referenced_files=5_001,
        last_completed_at=now,
        now=now.replace(hour=4),
    )
    assert not compaction_due(
        policy,
        referenced_files=5_000,
        last_completed_at=now,
        now=now,
    )
    assert not compaction_due(
        policy,
        referenced_files=5_000,
        last_completed_at=None,
        now=now.replace(hour=4),
    )


def test_scheduler_gates_before_attempting_compaction():
    from drover.server.lake.maintenance import compaction_skip_reason

    assert (
        compaction_skip_reason(
            maintenance_dsn_env="",
            planned_stop_active=False,
            exporter_backlog_high=False,
            maintenance_admitted=True,
        )
        == "maintenance_dsn_not_configured"
    )
    assert (
        compaction_skip_reason(
            maintenance_dsn_env="DROVER_LAKE_MAINTENANCE_DSN",
            planned_stop_active=True,
            exporter_backlog_high=False,
            maintenance_admitted=True,
        )
        == "planned_stop_active"
    )
    assert (
        compaction_skip_reason(
            maintenance_dsn_env="DROVER_LAKE_MAINTENANCE_DSN",
            planned_stop_active=False,
            exporter_backlog_high=True,
            maintenance_admitted=True,
        )
        == "exporter_backlog_high"
    )
    assert (
        compaction_skip_reason(
            maintenance_dsn_env="DROVER_LAKE_MAINTENANCE_DSN",
            planned_stop_active=False,
            exporter_backlog_high=False,
            maintenance_admitted=False,
        )
        == "foreground_or_maintenance_active"
    )


def test_maintenance_configuration_is_explicit_and_bounded():
    from drover.config import AnalyticsConfig

    assert AnalyticsConfig().maintenance_dsn_env == ""
    assert AnalyticsConfig().auto_compaction_soft_file_limit == 5_000
    with pytest.raises(ValueError, match="maintenance_dsn_env"):
        AnalyticsConfig(maintenance_dsn_env="dsn=secret")
    with pytest.raises(ValueError, match="auto_compaction_soft_file_limit"):
        AnalyticsConfig(auto_compaction_soft_file_limit=0)
    with pytest.raises(ValueError, match="auto_compaction_max_runtime_seconds"):
        AnalyticsConfig(auto_compaction_max_runtime_seconds=0)


def test_compact_cli_dry_run_is_honest_about_unprojected_after_count(tmp_path):
    from click.testing import CliRunner

    from drover.server.lake.cli import lake_cmd

    result = CliRunner().invoke(
        lake_cmd,
        [
            "compact",
            "--data-root",
            str(tmp_path),
            "--catalog-dsn-env",
            "DROVER_LAKE_ADMIN_DSN",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    plan = __import__("json").loads(result.output)
    assert plan["referenced_files_before"] is None
    assert plan["referenced_files_after"] is None
    assert plan["after_is_projected"] is False


def test_compact_cli_refuses_without_recent_verified_backup(tmp_path):
    from click.testing import CliRunner

    from drover.server.lake.cli import lake_cmd

    result = CliRunner().invoke(
        lake_cmd,
        [
            "compact",
            "--data-root",
            str(tmp_path),
            "--catalog-dsn-env",
            "DROVER_LAKE_ADMIN_DSN",
            "--backup-receipt",
            str(tmp_path / "missing.json"),
        ],
    )

    assert result.exit_code != 0
    assert "recent verified backup required" in result.output


def test_readyz_exposes_disabled_compaction_state(monkeypatch, tmp_path):
    import json

    from drover.server.metrics import MetricsCollector
    from drover.server.readiness import ReadinessProbe

    class Result:
        def as_response(self, **_kwargs):
            return 200, json.dumps({"stores": []})

    monkeypatch.setattr(ReadinessProbe, "check", lambda self: Result())
    lake = {
        "state": "warn",
        "referenced_files": 25_000,
        "referenced_file_limit": 50_000,
        "referenced_file_ratio": 0.5,
        "auto_compaction": "disabled",
        "detail": "auto-compaction disabled (maintenance DSN not configured)",
    }
    collector = MetricsCollector(
        duckdb_path=tmp_path / "db",
        incoming_dir=tmp_path,
        summarizer_report={},
        lake_health=lambda: lake,
    )

    _, body = collector.render_readiness()

    assert json.loads(body)["lake"] == lake


def test_scheduler_skips_planned_stop_before_starting_child(monkeypatch, tmp_path):
    from drover.server.analytics_maintenance import MaintenanceAdmission
    from drover.server.lake import maintenance
    from drover.server.lake.runtime import LakeSpec

    monkeypatch.setattr(
        maintenance,
        "referenced_file_health",
        lambda *_args, **_kwargs: {"state": "warn", "referenced_files": 5_001},
    )
    invoked = []
    scheduler = maintenance.LakeCompactionScheduler(
        reader_spec=LakeSpec("READER", tmp_path, tmp_path, "0" * 64),
        maintenance_dsn_env="MAINTENANCE",
        policy=maintenance.CompactionPolicy(soft_file_limit=5_000, daily_window_hour=3),
        max_runtime_seconds=10,
        maintenance_admission=MaintenanceAdmission(None),
        planned_stop_active=lambda: True,
        exporter_backlog_high=lambda: False,
        execute=lambda *args, **kwargs: invoked.append((args, kwargs)),
    )

    assert scheduler.tick(now=datetime(2026, 10, 20, 4, tzinfo=UTC)) is None
    assert invoked == []
    assert scheduler.health()["last_skip"] == "planned_stop_active"
