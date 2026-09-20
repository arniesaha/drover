"""Fenced DuckDB-to-PostgreSQL control-store import tests."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest
from click.testing import CliRunner


@pytest.fixture
def postgres_target(tmp_path: Path, monkeypatch):
    dsn = os.environ.get("DROVER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("DROVER_TEST_POSTGRES_DSN is required for PostgreSQL integration")
    from drover.config import ControlStoreConfig
    from drover.server.control_store import close_control_store, configure_control_store
    from drover.schema import bootstrap

    path = tmp_path / "target.duckdb"
    config = ControlStoreConfig(
        backend="postgres",
        dsn_env="DROVER_TEST_POSTGRES_DSN",
        pool_min_size=1,
        pool_max_size=2,
        acquire_timeout_seconds=5.0,
        statement_timeout_seconds=5.0,
        schema=f"drover_migration_{uuid4().hex}",
    )
    monkeypatch.setenv(config.dsn_env, dsn)
    configure_control_store(path, config)
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    try:
        yield path
    finally:
        close_control_store(path)
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'DROP SCHEMA IF EXISTS "{config.schema}" CASCADE')


def _legacy_snapshot(tmp_path: Path, *, created_at: datetime) -> Path:
    from drover.schema import bootstrap
    from drover.server.db import control_plane_path

    source = tmp_path / "legacy.duckdb"
    bootstrap(parquet_dir=tmp_path / "legacy-parquet", duckdb_path=source)
    with duckdb.connect(str(control_plane_path(source))) as con:
        con.execute(
            "INSERT INTO harness_hosts (host_id, display_name, kind, status, capabilities_json) "
            "VALUES ('legacy-host', 'Legacy Host', 'test', 'online', '{}')"
        )
        con.execute(
            "INSERT INTO harness_sessions (session_id, host_id, harness, command, status) "
            "VALUES ('legacy-session', 'legacy-host', 'codex', 'codex', 'completed')"
        )
        con.executemany(
            """
            INSERT INTO harness_events
              (event_id, session_id, event_type, payload_json, created_at, seq, dedup_key)
            VALUES (?, 'legacy-session', 'assistant_output', ?, ?, ?, ?)
            """,
            [
                ("legacy-null", '{"text":"first"}', created_at, None, "dedup-null"),
                ("legacy-repeat", '{"text":"second"}', created_at, 4, "dedup-repeat"),
                (
                    "legacy-repeat-2",
                    '{"text":"third"}',
                    created_at,
                    4,
                    "dedup-repeat-2",
                ),
            ],
        )
        con.execute(
            "INSERT INTO live_session_recaps (session_id, recap_text, source_seq, generated_at) "
            "VALUES ('legacy-session', 'Legacy recap', 4, ?)",
            [created_at],
        )
        con.execute(
            """
            INSERT INTO live_recap_jobs
              (session_id, desired_source_seq, status, attempts, enqueued_at, updated_at)
            VALUES ('legacy-session', 4, 'done', 1, ?, ?)
            """,
            [created_at, created_at],
        )
        con.execute(
            """
            INSERT INTO advisory_findings
              (finding_id, fingerprint, analyzer_id, rule_id, target_type, target_id,
               analyzer_class, severity, confidence, title, impact, remediation_json,
               state, first_seen_at, last_seen_at, latest_run_id)
            VALUES ('finding-1', 'fingerprint-1', 'test', 'rule', 'session',
                    'legacy-session', 'deterministic', 'high', 'confirmed', 'Title',
                    'Impact', '[]', 'open', ?, ?, 'run-1')
            """,
            [created_at, created_at],
        )
        con.execute(
            """
            INSERT INTO advisory_occurrences
              (occurrence_id, finding_id, run_id, outcome, observed_at, recorded_at)
            VALUES ('occurrence-1', 'finding-1', 'run-1', 'open', ?, ?)
            """,
            [created_at, created_at],
        )
        con.execute(
            """
            INSERT INTO session_usage
              (session_id, host_id, harness, input_tokens, output_tokens, turn_count,
               exact, source, source_seq, source_event_count, observed_at)
            VALUES ('legacy-session', 'legacy-host', 'codex', 10, 3, 2, TRUE,
                    'harness_events', 4, 3, ?)
            """,
            [created_at],
        )
        con.execute(
            """
            INSERT INTO session_usage_sources
              (source_usage_id, session_id, source, host_id, harness, input_tokens,
               output_tokens, turn_count, exact, usage_observed, source_seq,
               source_event_count, observed_at)
            VALUES ('usage-source-1', 'legacy-session', 'harness_events', 'legacy-host',
                    'codex', 10, 3, 2, TRUE, TRUE, 4, 3, ?)
            """,
            [created_at],
        )
        con.execute(
            """
            INSERT INTO native_usage_partition_totals
              (native_usage_partition_id, session_id, partition_date, input_tokens,
               turn_count, event_count, exact, observed_at)
            VALUES ('native-total-1', 'legacy-session', '2026-09-20', 10, 2, 3, TRUE, ?)
            """,
            [created_at],
        )
        con.execute(
            """
            INSERT INTO native_usage_partition_watermarks
              (partition_date, source_activity_at, rolled_at)
            VALUES ('2026-09-20', ?, ?)
            """,
            [created_at, created_at],
        )
    return control_plane_path(source)


def test_fenced_import_preserves_legacy_identity_timezone_and_event_order(
    postgres_target, tmp_path: Path
):
    """No PostgreSQL target is ready until its explicit snapshot verification passes."""
    from drover.server.control_migration import (
        control_store_ready,
        import_legacy_snapshot,
        verify_legacy_import,
    )
    from drover.server.db import control_plane_connection

    source = _legacy_snapshot(tmp_path, created_at=datetime(2026, 9, 20, 12, 0, 0))
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "control_server_identity": {"server_id": "legacy-server-id"},
                "control_credentials": [
                    {
                        "credential_id": "credential-1",
                        "scope": "device",
                        "label": "Legacy phone",
                        "verifier": "hash:legacy",
                        "created_at": "2026-09-20T12:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert control_store_ready(postgres_target) is False
    report = import_legacy_snapshot(
        postgres_target,
        source_snapshot=source,
        source_timezone="America/Los_Angeles",
        credential_document=credentials,
    )
    assert report["state"] == "ready"
    assert set(report["details"]["tables"]) == {
        "harness_hosts",
        "harness_sessions",
        "harness_events",
        "live_session_recaps",
        "live_recap_jobs",
        "advisory_findings",
        "advisory_occurrences",
        "session_usage",
        "session_usage_sources",
        "native_usage_partition_totals",
        "native_usage_partition_watermarks",
        "control_server_identity",
        "control_credentials",
    }
    assert all(item["match"] for item in report["details"]["tables"].values())
    assert control_store_ready(postgres_target) is True
    assert (
        verify_legacy_import(
            postgres_target,
            source_snapshot=source,
            source_timezone="America/Los_Angeles",
            credential_document=credentials,
        )["ok"]
        is True
    )

    with control_plane_connection(postgres_target) as con:
        assert con.execute(
            "SELECT event_id, seq, dedup_key FROM harness_events ORDER BY event_id"
        ).fetchall() == [
            ("legacy-null", None, "dedup-null"),
            ("legacy-repeat", 4, "dedup-repeat"),
            ("legacy-repeat-2", 4, "dedup-repeat-2"),
        ]
        assert con.execute(
            "SELECT payload_json FROM harness_event_payloads WHERE event_id = 'legacy-null'"
        ).fetchone() == ('{"text":"first"}',)
        assert (
            con.execute(
                "SELECT created_at FROM harness_events WHERE event_id = 'legacy-null'"
            )
            .fetchone()[0]
            .isoformat()
            == "2026-09-20T19:00:00+00:00"
        )
        assert con.execute(
            "SELECT identity_value FROM control_server_identity WHERE identity_key = 'server_id'"
        ).fetchone() == ("legacy-server-id",)
        assert con.execute("SELECT verifier FROM control_credentials").fetchone() == (
            "hash:legacy",
        )


def test_fenced_import_rejects_ambiguous_legacy_wall_time(
    postgres_target, tmp_path: Path
):
    """An ambiguous DST instant cannot silently change source event ordering."""
    from drover.server.control_migration import import_legacy_snapshot

    source = _legacy_snapshot(tmp_path, created_at=datetime(2026, 11, 1, 1, 30, 0))
    with pytest.raises(ValueError, match="ambiguous"):
        import_legacy_snapshot(
            postgres_target,
            source_snapshot=source,
            source_timezone="America/Los_Angeles",
        )


def test_interrupted_import_marks_target_failed_and_unready(
    postgres_target, tmp_path: Path
):
    """A failed all-table verification cannot silently expose partial migration data."""
    from drover.server.control_migration import (
        control_store_status,
        import_legacy_snapshot,
    )

    source = _legacy_snapshot(tmp_path, created_at=datetime(2026, 9, 20, 12, 0, 0))
    with duckdb.connect(str(source)) as con:
        con.execute(
            """
            INSERT INTO harness_events
              (event_id, session_id, event_type, payload_json, created_at, seq, dedup_key)
            VALUES ('orphan-event', 'missing-session', 'assistant_output', '{}', ?, 1, 'orphan')
            """,
            [datetime(2026, 9, 20, 12, 1, 0)],
        )

    with pytest.raises(RuntimeError, match="verification failed"):
        import_legacy_snapshot(
            postgres_target,
            source_snapshot=source,
            source_timezone="America/Los_Angeles",
        )
    status = control_store_status(postgres_target)
    assert status["state"] == "failed"
    assert status["ready"] is False


def test_empty_initialization_requires_an_explicit_empty_target(postgres_target):
    """Bootstrap alone is unready, and init refuses a target containing a host."""
    from drover.server.control_migration import (
        control_store_ready,
        initialize_empty_control_store,
    )
    from drover.server.harness.registry import HarnessRegistry

    assert control_store_ready(postgres_target) is False
    registry = HarnessRegistry(postgres_target)
    registry.register_host(host_id="not-empty", display_name="Host", kind="test")
    with pytest.raises(RuntimeError, match="populated"):
        initialize_empty_control_store(postgres_target)


def test_empty_initialization_marks_a_bootstrapped_empty_target_ready(postgres_target):
    """A schema exists first; an operator must make the empty-serving decision."""
    from drover.server.control_migration import (
        control_store_ready,
        initialize_empty_control_store,
    )

    assert control_store_ready(postgres_target) is False
    status = initialize_empty_control_store(postgres_target)
    assert status["state"] == "ready"
    assert status["mode"] == "empty"
    assert control_store_ready(postgres_target) is True


def test_control_store_cli_exposes_only_explicit_offline_lifecycle_commands():
    """The CLI must never auto-discover a live source or imply file rollback."""
    from drover.server.__main__ import main

    result = CliRunner().invoke(main, ["control-store", "--help"])

    assert result.exit_code == 0, result.output
    assert "init" in result.output
    assert "import" in result.output
    assert "verify" in result.output
    assert "status" in result.output
