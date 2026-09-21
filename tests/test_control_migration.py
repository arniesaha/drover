"""Fenced DuckDB-to-PostgreSQL control-store import tests."""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timezone
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
    from drover.schema import bootstrap
    from drover.server.control_store import close_control_store, configure_control_store

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
        # These legacy usage and native-rollup writers intentionally produced
        # naive UTC. Their values must not be shifted by the operator's local
        # source-timezone used for the old harness wall-clock columns above.
        assert (
            con.execute(
                "SELECT observed_at FROM session_usage WHERE session_id = 'legacy-session'"
            )
            .fetchone()[0]
            .isoformat()
            == "2026-09-20T12:00:00+00:00"
        )
        assert (
            con.execute(
                "SELECT observed_at FROM session_usage_sources WHERE source_usage_id = 'usage-source-1'"
            )
            .fetchone()[0]
            .isoformat()
            == "2026-09-20T12:00:00+00:00"
        )
        assert (
            con.execute(
                "SELECT observed_at FROM native_usage_partition_totals WHERE native_usage_partition_id = 'native-total-1'"
            )
            .fetchone()[0]
            .isoformat()
            == "2026-09-20T12:00:00+00:00"
        )
        assert con.execute(
            "SELECT source_activity_at, rolled_at FROM native_usage_partition_watermarks"
        ).fetchone() == (
            datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
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


def test_import_rejects_unknown_even_null_source_columns(
    postgres_target, tmp_path: Path
):
    """A verified import has a fenced source schema, not a lossy projection."""
    from drover.server.control_migration import import_legacy_snapshot

    source = _legacy_snapshot(tmp_path, created_at=datetime(2026, 9, 20, 12, 0, 0))
    with duckdb.connect(str(source)) as con:
        con.execute("ALTER TABLE harness_hosts ADD COLUMN future_null_only VARCHAR")

    with pytest.raises(
        ValueError, match="unsupported source columns.*future_null_only"
    ):
        import_legacy_snapshot(
            postgres_target,
            source_snapshot=source,
            source_timezone="America/Los_Angeles",
        )


def test_import_rejects_missing_load_bearing_source_columns(
    postgres_target, tmp_path: Path
):
    """An absent event envelope cannot be replaced by a synthetic empty payload."""
    from drover.server.control_migration import import_legacy_snapshot
    from drover.server.db import CONTROL_PLANE_TABLES

    source = _legacy_snapshot(tmp_path, created_at=datetime(2026, 9, 20, 12, 0, 0))
    missing_column_source = tmp_path / "missing-payload-json.duckdb"
    with duckdb.connect(str(missing_column_source)) as con:
        source_literal = str(source).replace("'", "''")
        con.execute(f"ATTACH '{source_literal}' AS legacy")
        for table in CONTROL_PLANE_TABLES:
            select = (
                "SELECT * EXCLUDE (payload_json) FROM legacy.harness_events"
                if table == "harness_events"
                else f"SELECT * FROM legacy.{table}"
            )
            con.execute(f"CREATE TABLE {table} AS {select}")
        con.execute("DETACH legacy")

    with pytest.raises(
        ValueError, match="missing required source columns.*payload_json"
    ):
        import_legacy_snapshot(
            postgres_target,
            source_snapshot=missing_column_source,
            source_timezone="America/Los_Angeles",
        )


def test_import_uses_portable_binary_event_order_for_streamed_verification(
    postgres_target, tmp_path: Path
):
    """DuckDB and PostgreSQL digest the mixed Unicode event-id set identically."""
    from drover.server.control_migration import (
        _iter_source_rows,
        _zone,
        import_legacy_snapshot,
        verify_legacy_import,
    )

    source = _legacy_snapshot(tmp_path, created_at=datetime(2026, 9, 20, 12, 0, 0))
    event_ids = ("A-event", "a-event", "-event", "_event", "évent")
    with duckdb.connect(str(source)) as con:
        con.executemany(
            """
            INSERT INTO harness_events
              (event_id, session_id, event_type, payload_json, created_at, seq, dedup_key)
            VALUES (?, 'legacy-session', 'assistant_output', ?, ?, 9, ?)
            """,
            [
                (
                    event_id,
                    json.dumps({"text": event_id}),
                    datetime(2026, 9, 20, 12, 2),
                    f"dedup-{index}",
                )
                for index, event_id in enumerate(event_ids)
            ],
        )

    source_order = [
        row["event_id"]
        for batch in _iter_source_rows(
            source, "harness_events", _zone("UTC"), batch_size=2
        )
        for row in batch
    ]
    assert source_order == sorted(
        source_order, key=lambda event_id: event_id.encode("utf-8")
    )
    assert (
        import_legacy_snapshot(
            postgres_target, source_snapshot=source, source_timezone="UTC"
        )["state"]
        == "ready"
    )
    assert (
        verify_legacy_import(
            postgres_target, source_snapshot=source, source_timezone="UTC"
        )["ok"]
        is True
    )


def test_import_fails_closed_when_source_changes_between_event_phases(
    postgres_target, tmp_path: Path, monkeypatch
):
    """One target cannot become ready from old metadata and later event history."""
    import drover.server.control_migration as migration

    source = _legacy_snapshot(tmp_path, created_at=datetime(2026, 9, 20, 12, 0, 0))
    original_iter = migration._iter_source_rows
    event_streams = 0

    def replace_source_after_first_event_stream(snapshot, table, source_zone, **kwargs):
        nonlocal event_streams
        if table == "harness_events":
            event_streams += 1
            if event_streams == 2:
                replacement = tmp_path / "changed-source.duckdb"
                shutil.copy2(source, replacement)
                with duckdb.connect(str(replacement)) as con:
                    con.execute(
                        """
                        INSERT INTO harness_events
                          (event_id, session_id, event_type, payload_json, created_at, seq, dedup_key)
                        VALUES ('changed-between-phases', 'legacy-session', 'assistant_output',
                                '{"text":"late source change"}', ?, 9, 'changed-between-phases')
                        """,
                        [datetime(2026, 9, 20, 12, 3)],
                    )
                os.replace(replacement, source)
        yield from original_iter(snapshot, table, source_zone, **kwargs)

    monkeypatch.setattr(
        migration, "_iter_source_rows", replace_source_after_first_event_stream
    )
    with pytest.raises(RuntimeError, match="fenced source inputs changed"):
        migration.import_legacy_snapshot(
            postgres_target, source_snapshot=source, source_timezone="UTC"
        )

    status = migration.control_store_status(postgres_target)
    assert status["state"] == "failed"
    assert status["ready"] is False


def test_import_fails_closed_when_credential_document_changes(
    postgres_target, tmp_path: Path, monkeypatch
):
    """A credential document is part of the fenced import input, not side data."""
    import drover.server.control_migration as migration

    source = _legacy_snapshot(tmp_path, created_at=datetime(2026, 9, 20, 12, 0, 0))
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "control_server_identity": {"server_id": "original"},
                "control_credentials": [],
            }
        ),
        encoding="utf-8",
    )
    original_rebuild = migration._rebuild_previews

    def mutate_credentials(con):
        credentials.write_text(
            json.dumps(
                {
                    "control_server_identity": {"server_id": "changed"},
                    "control_credentials": [],
                }
            ),
            encoding="utf-8",
        )
        original_rebuild(con)

    monkeypatch.setattr(migration, "_rebuild_previews", mutate_credentials)
    with pytest.raises(RuntimeError, match="fenced source inputs changed"):
        migration.import_legacy_snapshot(
            postgres_target,
            source_snapshot=source,
            source_timezone="UTC",
            credential_document=credentials,
        )

    status = migration.control_store_status(postgres_target)
    assert status["state"] == "failed"
    assert status["ready"] is False


def test_verify_rejects_source_mutation_after_a_matching_comparison(
    postgres_target, tmp_path: Path, monkeypatch
):
    """Read-only verification cannot certify a source that changed during it."""
    import drover.server.control_migration as migration

    source = _legacy_snapshot(tmp_path, created_at=datetime(2026, 9, 20, 12, 0, 0))
    assert (
        migration.import_legacy_snapshot(
            postgres_target, source_snapshot=source, source_timezone="UTC"
        )["state"]
        == "ready"
    )
    original_verify = migration._verify_rows

    def mutate_after_compare(*args, **kwargs):
        report = original_verify(*args, **kwargs)
        replacement = tmp_path / "verify-changed-source.duckdb"
        shutil.copy2(source, replacement)
        with duckdb.connect(str(replacement)) as con:
            con.execute(
                "UPDATE harness_events SET payload_json = ? WHERE event_id = ?",
                ['{"text":"changed"}', "legacy-null"],
            )
        os.replace(replacement, source)
        return report

    monkeypatch.setattr(migration, "_verify_rows", mutate_after_compare)
    with pytest.raises(RuntimeError, match="fenced source inputs changed"):
        migration.verify_legacy_import(
            postgres_target, source_snapshot=source, source_timezone="UTC"
        )
    # This previously verified import is still the only ready target state;
    # read-only verification cannot rewrite it.
    assert migration.control_store_status(postgres_target)["ready"] is True


def test_verify_accepts_an_unchanged_snapshot_copy_at_another_path(
    postgres_target, tmp_path: Path
):
    """Persistent content identity does not make a source pathname part of import."""
    import drover.server.control_migration as migration

    source = _legacy_snapshot(tmp_path, created_at=datetime(2026, 9, 20, 12, 0, 0))
    snapshot_copy = tmp_path / "unchanged-copy.duckdb"
    shutil.copy2(source, snapshot_copy)
    imported = migration.import_legacy_snapshot(
        postgres_target, source_snapshot=source, source_timezone="UTC"
    )

    assert (
        migration.verify_legacy_import(
            postgres_target, source_snapshot=snapshot_copy, source_timezone="UTC"
        )["ok"]
        is True
    )
    assert imported["source_fingerprint"] == migration._source_fingerprint(
        snapshot_copy, None
    )


def test_concurrent_snapshot_import_keeps_the_successful_owner_ready(
    postgres_target, tmp_path: Path, monkeypatch
):
    """A waiting second source cannot overwrite the first import's ready marker."""
    import drover.server.control_migration as migration

    source_a_dir = tmp_path / "source-a"
    source_b_dir = tmp_path / "source-b"
    source_a_dir.mkdir()
    source_b_dir.mkdir()
    source_a = _legacy_snapshot(
        source_a_dir, created_at=datetime(2026, 9, 20, 12, 0, 0)
    )
    source_b = _legacy_snapshot(
        source_b_dir, created_at=datetime(2026, 9, 20, 12, 0, 0)
    )
    with duckdb.connect(str(source_b)) as con:
        con.execute(
            "UPDATE harness_events SET payload_json = '{\"text\":\"other source\"}' WHERE event_id = 'legacy-null'"
        )

    entered_first = threading.Event()
    entered_second = threading.Event()
    release_first = threading.Event()
    original_rebuild = migration._rebuild_previews

    def pause_first_rebuild(con):
        if not entered_first.is_set():
            entered_first.set()
            assert release_first.wait(timeout=5)
        else:
            entered_second.set()
        original_rebuild(con)

    monkeypatch.setattr(migration, "_rebuild_previews", pause_first_rebuild)
    results: dict[str, object] = {}

    def run_import(label: str, source: Path) -> None:
        try:
            results[label] = migration.import_legacy_snapshot(
                postgres_target, source_snapshot=source, source_timezone="UTC"
            )
        except BaseException as exc:  # thread boundary retains the exact failure.
            results[label] = exc

    first = threading.Thread(target=run_import, args=("first", source_a))
    second = threading.Thread(target=run_import, args=("second", source_b))
    first.start()
    assert entered_first.wait(timeout=5)
    second.start()
    entered_second.wait(timeout=1)
    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert isinstance(results.get("first"), dict)
    assert results["first"]["state"] == "ready"
    assert isinstance(results.get("second"), RuntimeError)
    status = migration.control_store_status(postgres_target)
    assert status["state"] == "ready"
    assert status["source_fingerprint"] == results["first"]["source_fingerprint"]


def test_empty_init_waits_for_import_admission_and_preserves_import_readiness(
    postgres_target, tmp_path: Path, monkeypatch
):
    """Empty init cannot expose an import target before its verification commits."""
    import drover.server.control_migration as migration

    source = _legacy_snapshot(tmp_path, created_at=datetime(2026, 9, 20, 12, 0, 0))
    import_at_rebuild = threading.Event()
    release_import = threading.Event()
    empty_init_finished = threading.Event()
    original_rebuild = migration._rebuild_previews

    def pause_import(con):
        import_at_rebuild.set()
        assert release_import.wait(timeout=5)
        original_rebuild(con)

    monkeypatch.setattr(migration, "_rebuild_previews", pause_import)
    results: dict[str, object] = {}

    def import_snapshot() -> None:
        results["import"] = migration.import_legacy_snapshot(
            postgres_target, source_snapshot=source, source_timezone="UTC"
        )

    def initialize_empty() -> None:
        try:
            results["init"] = migration.initialize_empty_control_store(postgres_target)
        finally:
            empty_init_finished.set()

    importer = threading.Thread(target=import_snapshot)
    initializer = threading.Thread(target=initialize_empty)
    importer.start()
    assert import_at_rebuild.wait(timeout=5)
    initializer.start()
    assert empty_init_finished.wait(timeout=0.25) is False
    release_import.set()
    importer.join(timeout=10)
    initializer.join(timeout=10)

    assert results["import"]["state"] == "ready"
    assert results["import"]["mode"] == "import"
    assert results["init"]["state"] == "ready"
    assert results["init"]["mode"] == "import"
    assert (
        migration.control_store_status(postgres_target)["source_fingerprint"]
        == results["import"]["source_fingerprint"]
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


def test_credential_document_in_runtime_shape_is_rejected(tmp_path: Path):
    """A runtime credentials.json must not import as zero identity and zero credentials.

    The runtime file uses server_id/fleet_name/credentials. The importer reads
    control_server_identity/control_credentials. Accepting the runtime shape made
    both .get() calls return empty, so the import wrote no identity and no
    verifiers while verification still reported ok, because expected and actual
    were both derived from the same empty parse. The hub then generated a fresh
    random server_id and every paired device was silently revoked.
    """
    from zoneinfo import ZoneInfo

    from drover.server.control_migration import _credential_rows

    document = tmp_path / "credentials.json"
    document.write_text(
        json.dumps(
            {
                "version": 1,
                "server_id": "2bf7acd9-5ae0-4526-8373-363ceb413bb5",
                "fleet_name": "drover",
                "credentials": [
                    {
                        "id": "credential-1",
                        "scope": "device",
                        "label": "iPhone",
                        "verifier": "hash:legacy",
                        "created_at": "2026-09-20T12:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        _credential_rows(document, ZoneInfo("America/Los_Angeles"))

    message = str(excinfo.value)
    assert "control_server_identity" in message
    assert "control_credentials" in message
    # "fleet_name" cannot appear unless the error echoes the keys actually
    # present, so this cannot pass on a generic message the way a bare
    # "server_id" check did: that is a substring of "control_server_identity".
    assert "fleet_name" in message, "must name the unexpected top-level keys it found"


def test_credential_document_with_no_identity_and_no_credentials_is_rejected(
    tmp_path: Path,
):
    """Passing a document explicitly means importing something from it."""
    from zoneinfo import ZoneInfo

    from drover.server.control_migration import _credential_rows

    document = tmp_path / "empty.json"
    document.write_text(
        json.dumps({"control_server_identity": {}, "control_credentials": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        _credential_rows(document, ZoneInfo("America/Los_Angeles"))


def test_credential_document_may_be_omitted_entirely():
    """Omitting --credentials stays the supported no-credential path."""
    from zoneinfo import ZoneInfo

    from drover.server.control_migration import _credential_rows

    assert _credential_rows(None, ZoneInfo("America/Los_Angeles")) == {
        "control_server_identity": [],
        "control_credentials": [],
    }


def test_import_failure_names_the_orphan_events_that_caused_it(
    postgres_target, tmp_path: Path
):
    """An opaque verification failure is unactionable on a real hub.

    A long-lived hub sweeps harness_sessions independently of harness_events, so
    events outlive their session row. Verification correctly refuses to import,
    but reported only "import verification failed before readiness marker",
    which names neither the failing check nor how many rows tripped it.
    """
    from drover.server.control_migration import import_legacy_snapshot
    from drover.server.db import control_plane_path

    source = _legacy_snapshot(tmp_path, created_at=datetime(2026, 9, 20, 12, 0, 0))
    with duckdb.connect(str(control_plane_path(tmp_path / "legacy.duckdb"))) as con:
        con.execute(
            """
            INSERT INTO harness_events
              (event_id, session_id, event_type, payload_json, created_at, seq, dedup_key)
            VALUES ('orphan-1', 'swept-session', 'assistant_output', '{}', ?, 1, 'dedup-orphan')
            """,
            [datetime(2026, 9, 20, 12, 0, 0)],
        )

    with pytest.raises(RuntimeError) as excinfo:
        import_legacy_snapshot(
            postgres_target,
            source_snapshot=source,
            source_timezone="America/Los_Angeles",
            credential_document=None,
        )

    message = str(excinfo.value)
    assert "events_missing_session" in message
    assert "1" in message
