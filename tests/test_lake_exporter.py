"""Real exporter transactions on a private PG cluster and small fixture lake."""

from dataclasses import replace

import duckdb
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from test_control_outbox import postgres_control_store
from test_lake_runtime import lake_spec

from drover.server.db import control_plane_connection
from drover.server.harness.registry import HarnessRegistry
from drover.server.lake.export_guard import APPLICATION_PREFIX
from drover.server.lake.export_worker import export
from drover.server.lake.exporter import LakeOutboxExporter, provision_exporter
from drover.server.lake.rebuild import OUTBOX_SCHEMA
from drover.server.lake.rebuild_worker import LINEAGE, POLICY_SCHEMA
from drover.server.lake.runtime import (
    LakeError,
    configure_catalog,
    create_table,
    lake_connection,
)


@pytest.fixture
def export_lake(lake_spec):
    with lake_connection(lake_spec, read_only=False, create=True) as con:
        configure_catalog(con)
        create_table(con, "agent_events", POLICY_SCHEMA | LINEAGE, day_partition=True)
        create_table(con, "control_outbox_batches", OUTBOX_SCHEMA | LINEAGE)
    provision_exporter(lake_spec)
    return lake_spec


def seed(control_path, *, count=2):
    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="export-host", display_name="Export", kind="test")
    registry.create_session(
        host_id="export-host",
        harness="codex",
        command="codex",
        session_id="export-session",
    )
    for i in range(count):
        registry.append_event(
            session_id="export-session",
            event_id=f"event-{i}",
            event_type="assistant_output",
            payload={"text": f"full substantive payload {i}"},
            seq=i + 1,
            content_preview="short preview",
        )


def lake_counts(spec):
    with lake_connection(spec) as con:
        return [
            con.execute(f"SELECT count(*) FROM lake.{t}").fetchone()[0]
            for t in (
                "agent_events",
                "control_outbox_batches",
                "export_event_versions",
                "export_batch_receipts",
            )
        ]


class CrashBeforeAck(RuntimeError):
    pass


def test_crash_before_ack_replays_receipt_without_second_commit(
    export_lake, postgres_control_store, monkeypatch, tmp_path
):
    control_path, _ = postgres_control_store
    seed(control_path)
    original = LakeOutboxExporter(control_path=control_path, spec=export_lake)

    def crash(*args):
        raise CrashBeforeAck()

    monkeypatch.setattr(original, "_acknowledge", crash)
    with original, pytest.raises(CrashBeforeAck):
        original.run_once()
    assert lake_counts(export_lake) == [2, 2, 2, 1]
    with control_plane_connection(control_path) as control:
        assert control.execute(
            "SELECT state FROM control_outbox_batches"
        ).fetchone() == ("claimed",)
        assert control.execute(
            "SELECT receipt_sha256 FROM lake_export_batches"
        ).fetchone() == (None,)
        # Identity enrichment after a crash must not change frozen projection/hash.
        control.execute(
            "UPDATE harness_sessions SET branch='learned-later' WHERE session_id='export-session'"
        )
    with lake_connection(export_lake) as con:
        snapshot = con.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchone()[0]
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(
        pa.table({"id": ["orphan"]}), export_lake.data_root / "orphan.parquet"
    )
    with LakeOutboxExporter(control_path=control_path, spec=export_lake) as replacement:
        assert replacement.run_once() == {
            "exported": 2,
            "acknowledged": 2,
            "replayed": True,
        }
        assert replacement.run_once() == {
            "exported": 0,
            "acknowledged": 0,
            "replayed": False,
        }
    assert lake_counts(export_lake) == [2, 2, 2, 1]
    with lake_connection(export_lake) as con:
        assert (
            con.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]
            == snapshot
        )
        assert con.execute(
            "SELECT content FROM lake.agent_events ORDER BY id"
        ).fetchall() == [
            ("full substantive payload 0",),
            ("full substantive payload 1",),
        ]
    with control_plane_connection(control_path) as control:
        assert control.execute(
            "SELECT state,archive_path,content_sha256 FROM control_outbox_batches"
        ).fetchone() == ("acknowledged", None, None)
        assert control.execute(
            "SELECT count(*) FROM control_outbox_events WHERE state='acknowledged'"
        ).fetchone() == (2,)
        assert control.execute(
            "SELECT count(*) FROM harness_event_archives"
        ).fetchone() == (0,)
        assert control.execute(
            "SELECT count(*) FROM harness_event_payloads"
        ).fetchone() == (2,)


def test_two_exporters_are_fenced_for_whole_owner_lifetime(
    export_lake, postgres_control_store
):
    control_path, _ = postgres_control_store
    seed(control_path, count=1)
    with LakeOutboxExporter(control_path=control_path, spec=export_lake) as first:
        with pytest.raises(LakeError, match="lake_mutation_fenced"):
            with LakeOutboxExporter(control_path=control_path, spec=export_lake):
                pytest.fail("second exporter became active")
        assert first.run_once()["acknowledged"] == 1
        # The fence is retained between batches, rather than borrowed per query.
        with pytest.raises(LakeError, match="lake_mutation_fenced"):
            with LakeOutboxExporter(control_path=control_path, spec=export_lake):
                pytest.fail("second exporter became active")
    with LakeOutboxExporter(control_path=control_path, spec=export_lake) as successor:
        assert successor.run_once()["acknowledged"] == 0


def frozen(exporter):
    from datetime import datetime, timezone

    return exporter._input(datetime.now(timezone.utc))


def test_snapshot_guard_rejects_old_token_after_replacement(
    export_lake, postgres_control_store, monkeypatch
):
    control_path, _ = postgres_control_store
    seed(control_path, count=1)
    with LakeOutboxExporter(control_path=control_path, spec=export_lake) as old:
        document = frozen(old)
        stale_dsn = make_conninfo(
            export_lake.dsn(), application_name=APPLICATION_PREFIX + old.token
        )
        old.fence.connection.close()
        with LakeOutboxExporter(
            control_path=control_path, spec=export_lake
        ) as replacement:
            monkeypatch.setenv("DROVER_STALE_EXPORT_DSN", stale_dsn)
            stale_spec = replace(export_lake, catalog_dsn_env="DROVER_STALE_EXPORT_DSN")
            with pytest.raises(duckdb.Error, match="fence lost"):
                export(stale_spec, document)
            assert lake_counts(export_lake) == [0, 0, 0, 0]
            assert replacement.run_once()["acknowledged"] == 1
    assert lake_counts(export_lake) == [1, 1, 1, 1]


def test_atomic_rollback_leaves_no_rows_or_receipt(
    export_lake, postgres_control_store, monkeypatch
):
    control_path, _ = postgres_control_store
    seed(control_path, count=1)
    with LakeOutboxExporter(control_path=control_path, spec=export_lake) as exporter:
        document = frozen(exporter)
        monkeypatch.setenv(
            "DROVER_BATCH_EXPORT_DSN",
            make_conninfo(
                export_lake.dsn(), application_name=APPLICATION_PREFIX + exporter.token
            ),
        )
        batch_spec = replace(export_lake, catalog_dsn_env="DROVER_BATCH_EXPORT_DSN")

        def fail():
            raise CrashBeforeAck()

        with pytest.raises(CrashBeforeAck):
            export(batch_spec, document, before_commit=fail)
        assert lake_counts(export_lake) == [0, 0, 0, 0]
        assert exporter.run_once()["replayed"] is False
    assert lake_counts(export_lake) == [1, 1, 1, 1]


@pytest.mark.parametrize(
    "table,column",
    [("control_outbox_batches", "payload_json"), ("export_event_versions", "content")],
)
def test_recovery_rejects_payload_corruption_before_ack(
    export_lake, postgres_control_store, monkeypatch, table, column
):
    control_path, _ = postgres_control_store
    seed(control_path, count=1)
    with LakeOutboxExporter(control_path=control_path, spec=export_lake) as exporter:
        monkeypatch.setattr(
            exporter,
            "_acknowledge",
            lambda *args: (_ for _ in ()).throw(CrashBeforeAck()),
        )
        with pytest.raises(CrashBeforeAck):
            exporter.run_once()
    # Keeping the per-row stored hash must not conceal changed archived bytes.
    with lake_connection(export_lake, read_only=False) as con:
        con.execute(f"UPDATE lake.{table} SET {column}='changed'")
    with LakeOutboxExporter(control_path=control_path, spec=export_lake) as exporter:
        with pytest.raises(LakeError, match="receipt_content_mismatch"):
            exporter.run_once()
    with control_plane_connection(control_path) as con:
        assert con.execute("SELECT state FROM control_outbox_batches").fetchone() == (
            "claimed",
        )


def test_duplicate_keys_rank_before_merge(
    export_lake, postgres_control_store, monkeypatch
):
    control_path, _ = postgres_control_store
    seed(control_path)
    from drover.server.lake import exporter as module

    original = module.project_control_event

    def duplicates(row, session):
        event = original(row, session)
        event["dedup_key"] = "same-fingerprint"
        # Attribution beats the later timestamp/id. Exactly one source per key.
        if row["event_id"] == "event-0":
            event.update(repo_owner="owner", repo_name="repo")
        return event

    monkeypatch.setattr(module, "project_control_event", duplicates)
    with LakeOutboxExporter(control_path=control_path, spec=export_lake) as exporter:
        assert exporter.run_once() == {
            "exported": 1,
            "acknowledged": 2,
            "replayed": False,
        }
    assert lake_counts(export_lake) == [1, 2, 1, 1]
    with lake_connection(export_lake) as con:
        assert con.execute("SELECT id FROM lake.agent_events").fetchall() == [
            ("event-0",)
        ]


def test_unlock_is_detected_even_when_connection_stays_open(
    export_lake, postgres_control_store
):
    from drover.server.lake.fence import MUTATION_LOCK

    control_path, _ = postgres_control_store
    with LakeOutboxExporter(control_path=control_path, spec=export_lake) as exporter:
        exporter.fence.connection.execute(
            "SELECT pg_advisory_unlock(%s)", [MUTATION_LOCK]
        )
        with pytest.raises(LakeError, match="lake_fence_lost"):
            exporter.run_once()


def test_limited_exporter_role_can_activate_and_commit(
    lake_spec, postgres_control_store, monkeypatch
):
    from uuid import uuid4

    from psycopg import sql

    from drover.server.lake.catalog_roles import provision_catalog_roles

    with lake_connection(lake_spec, read_only=False, create=True) as con:
        configure_catalog(con)
        create_table(con, "agent_events", POLICY_SCHEMA | LINEAGE, day_partition=True)
        create_table(con, "control_outbox_batches", OUTBOX_SCHEMA | LINEAGE)
    roles = provision_catalog_roles(
        lake_spec, prefix="export_test_" + uuid4().hex[:12]
    )["roles"]
    try:
        provision_exporter(lake_spec, exporter_role=roles["exporter"])
        monkeypatch.setenv(
            "DROVER_LIMITED_EXPORT_DSN",
            make_conninfo(lake_spec.dsn(), options="-c role=" + roles["exporter"]),
        )
        spec = replace(lake_spec, catalog_dsn_env="DROVER_LIMITED_EXPORT_DSN")
        control_path, _ = postgres_control_store
        seed(control_path, count=1)
        with LakeOutboxExporter(control_path=control_path, spec=spec) as exporter:
            with pytest.raises(duckdb.Error, match="token required"):
                with lake_connection(spec, read_only=False) as con:
                    con.execute(
                        "INSERT INTO lake.control_outbox_batches (payload_json) VALUES ('unfenced')"
                    )
            assert exporter.run_once()["acknowledged"] == 1
            # No ability to change the ownership row or disable commit fencing.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                exporter.fence.connection.execute(
                    "UPDATE drover_lake_export.ownership SET token='forged'"
                )
        assert lake_counts(lake_spec) == [1, 1, 1, 1]
    finally:
        with psycopg.connect(lake_spec.dsn(), autocommit=True) as con:
            for role in roles.values():
                con.execute(
                    sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role))
                )
                con.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_invalid_payload_hash_never_reaches_lake(export_lake, postgres_control_store):
    control_path, _ = postgres_control_store
    seed(control_path, count=1)
    with control_plane_connection(control_path) as con:
        con.execute("UPDATE harness_event_payloads SET payload_sha256='invalid'")
    with LakeOutboxExporter(control_path=control_path, spec=export_lake) as exporter:
        with pytest.raises(LakeError, match="payload_hash_mismatch"):
            exporter.run_once()
    assert lake_counts(export_lake) == [0, 0, 0, 0]
    with control_plane_connection(control_path) as con:
        assert con.execute("SELECT count(*) FROM lake_export_batches").fetchone() == (
            0,
        )


def test_replacement_waits_for_validated_catalog_commit(
    export_lake, postgres_control_store
):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    control_path, _ = postgres_control_store
    entered = threading.Event()
    with LakeOutboxExporter(control_path=control_path, spec=export_lake) as old:
        # A real snapshot trigger acquires its shared ownership row lock inside
        # the catalog transaction. It stays held after the original owner dies.
        with psycopg.connect(
            make_conninfo(
                export_lake.dsn(), application_name=APPLICATION_PREFIX + old.token
            )
        ) as commit:
            commit.execute(
                "UPDATE public.ducklake_snapshot SET snapshot_time=snapshot_time WHERE snapshot_id=(SELECT max(snapshot_id) FROM public.ducklake_snapshot)"
            )
            old.fence.connection.close()

            def replace_owner():
                with LakeOutboxExporter(control_path=control_path, spec=export_lake):
                    entered.set()

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(replace_owner)
                deadline = time.monotonic() + 2
                with psycopg.connect(export_lake.dsn(), autocommit=True) as probe:
                    while time.monotonic() < deadline:
                        waiting = probe.execute(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE 'SELECT drover_lake_export.activate%%'"
                        ).fetchone()[0]
                        if waiting:
                            break
                        time.sleep(0.01)
                    assert waiting, "replacement did not reach ownership drain"
                    assert not entered.is_set()
                commit.commit()
                future.result(timeout=2)
                assert entered.is_set()


def test_runtime_mismatch_refuses_ownership_and_claim(
    export_lake, postgres_control_store
):
    control_path, _ = postgres_control_store
    seed(control_path, count=1)
    bad_spec = replace(export_lake, engine_sha256="bad")
    with pytest.raises(LakeError, match="engine_hash_mismatch"):
        with LakeOutboxExporter(control_path=control_path, spec=bad_spec):
            pytest.fail("mismatched engine started")
    with control_plane_connection(control_path) as con:
        assert con.execute("SELECT state FROM control_outbox_events").fetchone() == (
            "pending",
        )
        assert con.execute(
            "SELECT count(*) FROM control_outbox_batches"
        ).fetchone() == (0,)


@pytest.fixture
def unprovisioned_lake(lake_spec):
    with lake_connection(lake_spec, read_only=False, create=True) as con:
        configure_catalog(con)
        create_table(con, "agent_events", POLICY_SCHEMA | LINEAGE, day_partition=True)
        create_table(con, "control_outbox_batches", OUTBOX_SCHEMA | LINEAGE)
    return lake_spec


def _provision_cli(spec, monkeypatch, *, role="drover_export_group"):
    from click.testing import CliRunner

    from drover.server.lake import cli as lake_cli

    monkeypatch.setenv("DROVER_LAKE_EXTENSION_DIR", str(spec.extension_dir))
    monkeypatch.setenv("DROVER_LAKE_ENGINE_SHA256", spec.engine_sha256)
    return CliRunner().invoke(
        lake_cli.lake_cmd,
        [
            "provision-exporter",
            "--data-root",
            str(spec.data_root),
            "--catalog-dsn-env",
            spec.catalog_dsn_env,
            "--exporter-role",
            role,
        ],
    )


def test_provision_exporter_cli_success_then_already_provisioned(
    unprovisioned_lake, postgres_dsn, monkeypatch
):
    import json

    from psycopg import sql

    spec = unprovisioned_lake
    role = "drover_export_cli_" + spec.data_root.parent.name[-8:].replace("-", "_")
    with psycopg.connect(postgres_dsn, autocommit=True) as con:
        con.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
    try:
        result = _provision_cli(spec, monkeypatch, role=role)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["provisioned"] is True
        assert spec.dsn() not in result.output
        again = _provision_cli(spec, monkeypatch, role=role)
        assert again.exit_code != 0
        assert "lake_export_already_provisioned" in again.output
    finally:
        with psycopg.connect(spec.dsn(), autocommit=True) as con:
            con.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        with psycopg.connect(postgres_dsn, autocommit=True) as con:
            con.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_provision_exporter_cli_reports_missing_tables(lake_spec, monkeypatch):
    with lake_connection(lake_spec, read_only=False, create=True) as con:
        configure_catalog(con)
    result = _provision_cli(lake_spec, monkeypatch)
    assert result.exit_code != 0
    assert "lake_export_tables_missing" in result.output


def test_provision_exporter_cli_requires_runtime_env(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from drover.server.lake import cli as lake_cli

    monkeypatch.delenv("DROVER_LAKE_EXTENSION_DIR", raising=False)
    monkeypatch.delenv("DROVER_LAKE_ENGINE_SHA256", raising=False)
    monkeypatch.setattr(
        lake_cli,
        "provision_exporter",
        lambda *a, **k: pytest.fail("must not provision without pinned runtime"),
        raising=False,
    )
    result = CliRunner().invoke(
        lake_cli.lake_cmd,
        [
            "provision-exporter",
            "--data-root",
            str(tmp_path),
            "--catalog-dsn-env",
            "X",
            "--exporter-role",
            "r",
        ],
    )
    assert result.exit_code != 0
    assert "DROVER_LAKE_EXTENSION_DIR" in result.output


def test_provision_exporter_cli_sanitizes_unexpected_errors(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from drover.server.lake import cli as lake_cli
    from drover.server.lake import exporter

    def boom(spec, exporter_role=None):
        raise psycopg.OperationalError(
            "connection failed: postgresql://admin:hunter22@db:5432/x"
        )

    monkeypatch.setattr(exporter, "provision_exporter", boom)
    monkeypatch.setenv("DROVER_LAKE_EXTENSION_DIR", str(tmp_path))
    monkeypatch.setenv("DROVER_LAKE_ENGINE_SHA256", "0")
    monkeypatch.setenv("X", "postgresql://admin:hunter22@db:5432/x")
    result = CliRunner().invoke(
        lake_cli.lake_cmd,
        [
            "provision-exporter",
            "--data-root",
            str(tmp_path),
            "--catalog-dsn-env",
            "X",
            "--exporter-role",
            "r",
        ],
    )
    assert result.exit_code != 0
    assert "lake_export_provision_failed" in result.output
    assert "hunter22" not in result.output
    assert "hunter22" not in (tmp_path / "admin-error.log").read_text()


def test_lifecycle_unprovisioned_catalog_yields_specific_code_and_logs_cause(
    unprovisioned_lake, postgres_control_store, monkeypatch, caplog
):
    import logging
    import threading
    from types import SimpleNamespace

    from drover.server.lake import lifecycle

    control_path, _ = postgres_control_store
    config = SimpleNamespace(
        duckdb_path=control_path,
        analytics=SimpleNamespace(
            retire_legacy_writers=False,
            exporter_dsn_env=unprovisioned_lake.catalog_dsn_env,
        ),
    )
    monkeypatch.setattr(lifecycle, "lake_spec", lambda *a, **k: unprovisioned_lake)
    with caplog.at_level(logging.WARNING, logger=lifecycle.log.name):
        runner = lifecycle.ExporterLifecycle(config)
        with pytest.raises(LakeError) as caught:
            runner.start(shutdown_event=threading.Event())
    assert caught.value.code == "lake_export_not_provisioned"
    assert runner.health()["last_error"] == "lake_export_not_provisioned"
    text = caplog.text
    assert "drover_lake_export" in text
    assert unprovisioned_lake.dsn() not in text
    assert len(text) < 1000


def test_lifecycle_unclassified_failure_keeps_stable_code_and_cleans_cause(
    tmp_path, monkeypatch, caplog
):
    import logging
    import threading
    from types import SimpleNamespace

    from drover.server.lake import lifecycle

    monkeypatch.setenv("LC_DSN", "postgresql://u:hunter22@h/db")

    class Boom:
        def __init__(self, **kwargs):
            raise psycopg.OperationalError(
                "bad conninfo password=hunter22 host=h\nsecond line"
            )

    monkeypatch.setattr(lifecycle, "LakeOutboxExporter", Boom)
    monkeypatch.setattr(lifecycle, "lake_spec", lambda *a, **k: None)
    config = SimpleNamespace(
        duckdb_path=tmp_path, analytics=SimpleNamespace(exporter_dsn_env="LC_DSN")
    )
    with caplog.at_level(logging.WARNING, logger=lifecycle.log.name):
        runner = lifecycle.ExporterLifecycle(config)
        with pytest.raises(LakeError) as caught:
            runner.start(shutdown_event=threading.Event())
    assert caught.value.code == "lake_export_unavailable"
    assert "OperationalError: bad conninfo" in caplog.text
    assert "hunter22" not in caplog.text
    assert "second line" not in caplog.text


def test_guard_missing_classifies_real_postgres_errors(postgres_dsn):
    """Needs only a private PostgreSQL, not the pinned DuckLake extensions."""
    from drover.server.lake.lifecycle import _guard_missing

    with psycopg.connect(postgres_dsn, autocommit=True) as con:
        with pytest.raises(psycopg.Error) as caught:
            con.execute("SELECT drover_lake_export.activate('t')")
        assert _guard_missing(caught.value)
        with pytest.raises(psycopg.Error) as other:
            con.execute("SELECT * FROM no_such_table_here")
        assert not _guard_missing(other.value)


def test_lake_exports_one_row_over_target_alone_then_drains_short_event(
    export_lake, postgres_control_store, tmp_path
):
    import json

    from drover.server.control_outbox import (
        MAX_INPUT_BYTES,
        TARGET_BATCH_BYTES,
        canonical_payload,
    )
    from drover.server.ingest import ingest_file

    path, legacy = postgres_control_store
    source = tmp_path / "large.jsonl"
    events = [
        dict(
            id=f"collector-{i}",
            session_id="collector",
            agent_id="host",
            timestamp=f"2026-10-04T12:00:0{i}+00:00",
            event_type="user_message",
            message=dict(
                role="user", content=("x" * (9 * 1024**2) if i == 0 else "short")
            ),
            raw_data={},
        )
        for i in range(2)
    ]

    source.write_text("".join(json.dumps(e) + "\n" for e in events))
    ingest_file(source, parquet_dir=legacy, duckdb_path=path)
    with LakeOutboxExporter(control_path=path, spec=export_lake) as exporter:
        document = frozen(exporter)
        assert document["event_ids"] == ["collector-0"]
        assert (
            TARGET_BATCH_BYTES
            < len(canonical_payload(document).encode())
            < MAX_INPUT_BYTES
        )
        assert exporter.run_once()["acknowledged"] == 1
        assert exporter.run_once()["acknowledged"] == 1
        assert exporter.run_once()["acknowledged"] == 0
    from drover.server.legacy_outbox import replay_legacy

    with control_plane_connection(path) as control:
        control.execute("DELETE FROM harness_event_payloads")
    rollback = tmp_path / "rollback"
    assert replay_legacy(path, rollback) == 2
    assert replay_legacy(path, rollback) == 0
    assert lake_counts(export_lake) == [2, 2, 2, 2]


def test_export_updates_activity_daily_in_its_single_snapshot(
    export_lake, postgres_control_store
):
    """The cockpit rollup must never lag the canonical event publication."""
    control_path, _ = postgres_control_store
    seed(control_path, count=2)
    with lake_connection(export_lake) as con:
        before = con.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchone()[0]
    with LakeOutboxExporter(control_path=control_path, spec=export_lake) as exporter:
        assert exporter.run_once()["acknowledged"] == 2
    with lake_connection(export_lake) as con:
        after = con.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[
            0
        ]
        assert after == before + 1
        assert con.execute(
            "SELECT session_id, sum(event_count) FROM lake.activity_daily GROUP BY session_id"
        ).fetchall() == [("export-session", 2)]


@pytest.mark.parametrize("failure", ["task_exit", "deadline"])
def test_supervised_crash_replays_and_drains_without_duplicates(
    export_lake, postgres_control_store, monkeypatch, failure
):
    """Kill the owner after lake commit; its replacement must replay the receipt."""
    import threading
    from types import SimpleNamespace

    from drover.server.lake import lifecycle, task_projection

    path, _ = postgres_control_store
    seed(path)
    monkeypatch.setattr(lifecycle, "lake_spec", lambda *a, **k: export_lake)
    monkeypatch.setattr(lifecycle, "_check_export_catalog", lambda *a: None)
    monkeypatch.setattr(lifecycle.ExporterLifecycle, "_checkpoint", lambda *a: None)
    monkeypatch.setattr(task_projection, "refresh_if_provisioned", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "RESTART_BACKOFF_SECONDS", 0.01)
    original = LakeOutboxExporter._acknowledge
    calls, replayed = [], []
    drained = threading.Event()
    released = []
    original_exit = LakeOutboxExporter.__exit__

    def exit_owner(self, *args):
        original_exit(self, *args)
        released.append(self.owner)

    def acknowledge(self, document, receipt, now):
        calls.append(self.owner)
        if len(calls) == 1:
            if failure == "task_exit":
                raise SystemExit("test owner killed")
            raise LakeError("lake_export_deadline")
        assert released == [calls[0]]
        original(self, document, receipt, now)
        drained.set()

    original_publish = LakeOutboxExporter._publish

    def publish(self, document):
        result = original_publish(self, document)
        replayed.append(result["replayed"])
        return result

    monkeypatch.setattr(LakeOutboxExporter, "__exit__", exit_owner)
    monkeypatch.setattr(LakeOutboxExporter, "_acknowledge", acknowledge)
    monkeypatch.setattr(LakeOutboxExporter, "_publish", publish)
    worker = lifecycle.ExporterLifecycle(
        SimpleNamespace(
            duckdb_path=path, analytics=SimpleNamespace(retire_legacy_writers=False)
        )
    )
    try:
        worker.start(shutdown_event=threading.Event())
        assert drained.wait(15), worker.health()
        assert worker.health()["running"]
        assert worker.health()["restart_count"] == 1
    finally:
        worker.stop()
    assert calls[0] != calls[1]
    assert replayed == [False, True]
    assert lake_counts(export_lake) == [2, 2, 2, 1]
    from drover.server.lake.freshness import read_freshness

    health = read_freshness(path)
    assert health["unacknowledged_batches"] == 0
    assert health["last_successful_export_at"] is not None
    with control_plane_connection(path) as con:
        assert con.execute(
            "SELECT count(*) FROM control_outbox_events WHERE state='acknowledged'"
        ).fetchone() == (2,)


def test_live_hung_batch_cancelled_then_receipt_replayed(
    export_lake, postgres_control_store, monkeypatch
):
    """A living owner stuck after commit must release before receipt replay."""
    import threading
    from types import SimpleNamespace

    from drover.server.lake import lifecycle, task_projection

    path, _ = postgres_control_store
    seed(path)
    monkeypatch.setattr(lifecycle, "lake_spec", lambda *a, **k: export_lake)
    monkeypatch.setattr(lifecycle, "_check_export_catalog", lambda *a: None)
    monkeypatch.setattr(lifecycle.ExporterLifecycle, "_checkpoint", lambda *a: None)
    monkeypatch.setattr(task_projection, "refresh_if_provisioned", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "FRESHNESS_POLL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(lifecycle, "RESTART_BACKOFF_SECONDS", 0.01)
    original = LakeOutboxExporter._acknowledge
    hung, drained = threading.Event(), threading.Event()
    owners = []

    def acknowledge(self, document, receipt, now):
        if not owners:
            owners.append(self.owner)
            hung.set()
            # Cooperative fake hangs in the real receipt/ack boundary. This
            # would never drain if the watchdog only reported stalled state.
            for _ in range(1000):
                if getattr(self, "cancelled", False):
                    self._check()
                if drained.wait(0.01):
                    return
            raise AssertionError("watchdog never cancelled living owner")
        assert self.owner != owners[0]
        original(self, document, receipt, now)
        drained.set()

    monkeypatch.setattr(LakeOutboxExporter, "_acknowledge", acknowledge)
    worker = lifecycle.ExporterLifecycle(
        SimpleNamespace(
            duckdb_path=path,
            analytics=SimpleNamespace(
                retire_legacy_writers=False, exporter_recovery_deadline_seconds=1
            ),
        )
    )
    try:
        worker.start(shutdown_event=threading.Event())
        assert hung.wait(5)
        assert drained.wait(5), worker.health()
        assert worker.health()["restart_count"] == 1
        assert worker.health()["last_error"] == "lake_export_recovery_deadline"
    finally:
        drained.set()
        worker.stop()
    assert lake_counts(export_lake) == [2, 2, 2, 1]
    with control_plane_connection(path) as con:
        assert con.execute(
            "SELECT count(*) FROM control_outbox_events WHERE state='acknowledged'"
        ).fetchone() == (2,)


def test_exporter_cancel_interrupts_borrowed_postgres_query(
    export_lake, postgres_control_store
):
    """Cancellation must interrupt DB waits and leave the pool usable."""
    import threading
    import time

    path, _ = postgres_control_store
    started, finished = threading.Event(), threading.Event()
    backend, errors = [], []
    with LakeOutboxExporter(control_path=path, spec=export_lake) as exporter:

        def query():
            try:
                with exporter._control() as control:
                    backend.append(control._connection.info.backend_pid)
                    started.set()
                    control.execute("SELECT pg_sleep(10) /* exporter_cancel_test */")
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        task = threading.Thread(target=query)
        task.start()
        try:
            assert started.wait(3)
            deadline = time.monotonic() + 3
            waiting = False
            while time.monotonic() < deadline:
                with control_plane_connection(path) as con:
                    (waiting,) = con.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid=? AND state='active' AND query LIKE '%exporter_cancel_test%')",
                        [backend[0]],
                    ).fetchone()
                if waiting:
                    break
                time.sleep(0.01)
            assert waiting, "query never became active"
            exporter.cancel()
            assert finished.wait(2), "cancellation did not interrupt DB query"
            assert len(errors) == 1
            assert isinstance(errors[0], psycopg.errors.QueryCanceled)
        finally:
            exporter.cancel()
            task.join(12)
    with control_plane_connection(path) as con:
        assert con.execute("SELECT 1").fetchone() == (1,)
