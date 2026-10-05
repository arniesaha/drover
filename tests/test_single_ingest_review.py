"""S2 review: collector lifetime, dead letters, pruning and projection progress."""

import json
from datetime import datetime, timedelta, timezone

import duckdb
import pytest
from test_control_outbox import postgres_control_store
from test_lake_exporter import export_lake
from test_lake_runtime import lake_spec
from test_lake_serving import verified_lake
from test_single_ingest_path import source

from drover.schema import bootstrap
from drover.server.control_exporter import ControlOutboxExporter
from drover.server.control_outbox import (
    LocalVerifiedArchiveResolver,
    outbox_status,
    prune_acknowledged_outbox,
    prune_verified_payloads,
)
from drover.server.db import control_plane_connection, open_duckdb_connection
from drover.server.harness.registry import HarnessRegistry
from drover.server.ingest import ingest_file
from drover.server.legacy_outbox import replay_legacy
from drover.server.mcp.tools import drover_fleet_status
from drover.server.memory_identity import refresh_memory_projection


def ingest_and_export(path, tmp_path, *, count=2):
    parquet = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet, duckdb_path=path)
    ingest_file(source(tmp_path, count=count), parquet_dir=parquet, duckdb_path=path)
    exporter = ControlOutboxExporter(
        control_path=path, analytical_path=path, parquet_dir=parquet, batch_size=1
    )
    for _ in range(count):
        exporter.run_once()
    return parquet, exporter


def finish_summaries(control):
    control.execute(
        "UPDATE pipeline_jobs SET status='succeeded' WHERE job_kind='summarize_session'"
    )


def test_collector_lifecycle_and_registry_fleet_lists(pg_control_path, tmp_path):
    path = source(tmp_path, count=2)
    ingest_file(path, parquet_dir=tmp_path / "unused", duckdb_path=pg_control_path)
    registry = HarnessRegistry(pg_control_path)
    assert registry.list_hosts(include_retired=True) == []
    assert registry.list_sessions() == []
    from drover.server.harness.usage_rollup import load_pending_candidates
    from drover.server.session_history import (
        HistoryQuery,
        fetch_history_facets,
        fetch_history_page,
    )

    assert fetch_history_page(pg_control_path, HistoryQuery())["items"] == []
    assert fetch_history_facets(pg_control_path)["hosts"] == []
    with control_plane_connection(pg_control_path) as control:
        assert load_pending_candidates(control, limit=100) == []
    assert drover_fleet_status(duckdb_path=pg_control_path)["active_sessions"] == []
    with control_plane_connection(pg_control_path) as control:
        row = control.execute(
            "SELECT status,started_at,ended_at,last_activity FROM harness_sessions"
        ).fetchone()
        assert row == (
            "completed",
            datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            datetime(2026, 10, 4, 12, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 4, 12, 0, 1, tzinfo=timezone.utc),
        )
        assert control.execute("SELECT status FROM harness_hosts").fetchone() == (
            "offline",
        )
    later = json.loads(source(tmp_path, session="s2-session").read_text())
    later.update(id="later", timestamp="2026-10-05T12:00:00Z")
    path.write_text(json.dumps(later) + "\n")
    ingest_file(path, parquet_dir=tmp_path / "unused", duckdb_path=pg_control_path)
    with control_plane_connection(pg_control_path) as control:
        assert control.execute(
            "SELECT status,ended_at,last_activity FROM harness_sessions"
        ).fetchone() == (
            "completed",
            datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
            datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
        )
        # Even a row from the pre-review implementation cannot block retirement.
        control.execute("UPDATE harness_sessions SET status='running',ended_at=NULL")
        control.execute("UPDATE harness_hosts SET status='online'")
    assert registry.list_sessions(archived_limit=0) == []
    assert registry.list_hosts() == []
    assert (
        registry.retire_host("s2-host", reason="collector metadata").retired_at
        is not None
    )


def test_collector_payload_prunes_then_retained_outbox_expires(
    pg_control_path, tmp_path
):
    parquet, exporter = ingest_and_export(pg_control_path, tmp_path, count=1)
    with open_duckdb_connection(pg_control_path) as analytics:
        assert analytics.execute(
            "SELECT count(*) FROM control_memory_events"
        ).fetchone() == (0,)
        assert analytics.execute("SELECT count(*) FROM agent_events").fetchone() == (1,)
    assert exporter._prune_verified_payloads()["protected_dependency"] == 1
    with control_plane_connection(pg_control_path) as control:
        finish_summaries(control)
    assert exporter._prune_verified_payloads()["pruned"] == 1
    with control_plane_connection(pg_control_path) as control:
        assert control.execute(
            "SELECT count(*) FROM harness_event_payloads"
        ).fetchone() == (0,)
        assert control.execute(
            "SELECT count(*) FROM harness_event_archives"
        ).fetchone() == (1,)
        now = datetime.now(timezone.utc)
        control.execute(
            "UPDATE control_outbox_events SET acknowledged_at=?",
            [now - timedelta(days=13)],
        )
        assert prune_acknowledged_outbox(control, now=now) == 0
        control.execute(
            "UPDATE control_outbox_events SET acknowledged_at=?",
            [now - timedelta(days=15)],
        )
        assert prune_acknowledged_outbox(control, now=now) == 1
    assert (parquet / "agent_events").exists()


def test_oversized_delivery_is_rejected_without_blocking_small_event(
    pg_control_path, tmp_path, caplog
):
    parquet = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet, duckdb_path=pg_control_path)
    ingest_file(
        source(tmp_path, content="x" * (33 * 1024**2)),
        parquet_dir=parquet,
        duckdb_path=pg_control_path,
    )
    ingest_file(
        source(tmp_path, count=2, content="small event"),
        parquet_dir=parquet,
        duckdb_path=pg_control_path,
    )
    exporter = ControlOutboxExporter(
        control_path=pg_control_path,
        analytical_path=pg_control_path,
        parquet_dir=parquet,
    )
    result = exporter.run_once(now=datetime.now(timezone.utc) + timedelta(seconds=10))
    assert result["pending"] == 0
    assert result["rejected"] == 1
    with control_plane_connection(pg_control_path) as control:
        rejected = control.execute(
            "SELECT event_id,state,rejection_reason,rejected_at FROM control_outbox_events WHERE state='rejected'"
        ).fetchone()
        assert rejected[0:2] == ("s2-event-0", "rejected")
        assert "MAX_INPUT_BYTES=33554432" in rejected[2] and rejected[3] is not None
        assert outbox_status(control)["acknowledged"] == 1
    assert "s2-event-0" in caplog.text and "Rejected" in caplog.text
    with duckdb.connect() as analytics:
        assert analytics.execute(
            "SELECT id FROM read_parquet(?)",
            [str(parquet / "agent_events/**/*.parquet")],
        ).fetchall() == [("s2-event-1",)]
    assert exporter.run_once()["pending"] == 0
    assert replay_legacy(pg_control_path, parquet) == 0


def test_prune_cursor_stops_at_processed_prefix(pg_control_path, tmp_path, monkeypatch):
    from drover.server import control_outbox

    parquet, _ = ingest_and_export(pg_control_path, tmp_path)
    with control_plane_connection(pg_control_path) as control:
        finish_summaries(control)
        manifests = {
            r[0]
            for r in control.execute(
                "SELECT batch_id FROM control_outbox_batches"
            ).fetchall()
        }
    base = LocalVerifiedArchiveResolver(parquet, lambda: manifests)
    clock = [0.0]
    cursors = []

    class SlowResolver:
        def resolve(self, **kwargs):
            value = base.resolve(**kwargs)
            clock[0] = 2.0
            return value

    monkeypatch.setattr(control_outbox.time, "monotonic", lambda: clock[0])
    first = prune_verified_payloads(
        pg_control_path,
        resolver=SlowResolver(),
        time_budget_seconds=1,
        cursor_callback=cursors.append,
    )
    assert first["pruned"] == 1
    assert cursors == ["s2-event-0"]
    clock[0] = 0
    second = prune_verified_payloads(
        pg_control_path,
        resolver=SlowResolver(),
        after=cursors[-1],
        time_budget_seconds=1,
        cursor_callback=cursors.append,
    )
    assert second["pruned"] == 1
    assert cursors == ["s2-event-0", "s2-event-1"]


def test_orphans_do_not_starve_harness_projection(tmp_path):
    from drover.server.memory_identity import ensure_memory_schema

    with duckdb.connect(str(tmp_path / "projection.duckdb")) as analytics:
        ensure_memory_schema(analytics)
        analytics.execute(
            "CREATE TABLE tasks(task_id VARCHAR PRIMARY KEY,repo_owner VARCHAR,repo_name VARCHAR,branch VARCHAR,principal_id VARCHAR,status VARCHAR,created_at TIMESTAMPTZ,last_activity_at TIMESTAMPTZ,session_count BIGINT,total_cost_usd DOUBLE,title VARCHAR)"
        )
        analytics.execute("""CREATE TABLE harness_exported_events AS
            SELECT 'orphan-' || i AS event_id, 'missing' AS session_id,
              'user_input' AS event_type, 'user_input' AS normalized_type, 'codex' AS normalized_source,
              '{"text":"orphan"}' AS payload_json, TIMESTAMPTZ '2026-01-01' AS created_at, i AS seq
            FROM range(1001) t(i)""")
        analytics.execute(
            "INSERT INTO harness_exported_events VALUES ('valid','known','user_input','user_input','codex','{\"text\":\"valid text\"}',TIMESTAMPTZ '2026-02-01',0)"
        )
        sessions = {
            "known": dict(
                session_id="known",
                native_session_id=None,
                summary_session_id=None,
                harness="codex",
                task_id="task",
                command="test",
                repo_owner=None,
                repo_name=None,
                branch=None,
                cwd=None,
            )
        }
        refresh_memory_projection(analytics, sessions)
        assert analytics.execute("SELECT id FROM control_memory_events").fetchall() == [
            ("valid",)
        ]


def test_old_published_batch_is_visible_in_status_and_logs(
    pg_control_path, tmp_path, caplog
):
    parquet = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet, duckdb_path=pg_control_path)
    with control_plane_connection(pg_control_path) as control:
        control.execute(
            "INSERT INTO control_outbox_batches(batch_id,state,member_count,published_at) VALUES('stuck','published',0,?)",
            [datetime.now(timezone.utc) - timedelta(minutes=6)],
        )
        assert outbox_status(control)["stalled_published_batches"] == 1
    # No member can satisfy acknowledgement; preserve the diagnostic evidence.
    exporter = ControlOutboxExporter(
        control_path=pg_control_path,
        analytical_path=pg_control_path,
        parquet_dir=parquet,
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(exporter, "_rebuild_relation_and_acknowledge", lambda **kw: 0)
    # Recovery needs real archive members; isolate the liveness diagnostic path.
    from drover.server import control_exporter

    monkey.setattr(control_exporter, "_claim_rows", lambda *a: [])
    try:
        assert exporter.run_once()["stalled_published_batches"] == 1
    finally:
        monkey.undo()
    assert "unacknowledged after 5 minutes" in caplog.text


def test_lake_fleet_filters_collectors_before_snapshot_limit(verified_lake):
    from drover.server.lake.read_models import read_model
    from drover.server.lake.serving import configure_analytics

    spec, path, config = verified_lake
    with control_plane_connection(path) as control:
        control.execute(
            "INSERT INTO harness_hosts(host_id,display_name,kind,status,capabilities_json) VALUES('collector','collector','collector','online','{}')"
        )
        control.execute(
            """INSERT INTO harness_sessions(session_id,host_id,harness,command,status,started_at)
            SELECT 'collector-' || i, 'collector', 'codex', 'collector', 'running', now()
            FROM generate_series(0,10000) t(i)"""
        )
    configure_analytics(path, config)
    assert read_model(path, "fleet")["active_sessions"] == []


def test_lake_rejects_huge_row_then_acknowledges_small(
    export_lake, postgres_control_store, tmp_path
):
    from drover.server.lake.exporter import LakeOutboxExporter
    from drover.server.lake.runtime import lake_connection

    path, _ = postgres_control_store
    ingest_file(
        source(tmp_path, content="x" * (17 * 1024**2)),
        parquet_dir=tmp_path / "unused",
        duckdb_path=path,
    )
    ingest_file(
        source(tmp_path, count=2, content="small"),
        parquet_dir=tmp_path / "unused",
        duckdb_path=path,
    )
    with LakeOutboxExporter(control_path=path, spec=export_lake) as exporter:
        assert exporter.run_once()["acknowledged"] == 1
        assert exporter.run_once()["acknowledged"] == 0
    with control_plane_connection(path) as control:
        status = outbox_status(control)
        assert status["rejected"] == 1 and status["acknowledged"] == 1
        assert status["pending"] == 0
    with lake_connection(export_lake) as lake:
        assert lake.execute("SELECT id FROM lake.agent_events").fetchall() == [
            ("s2-event-1",)
        ]


def test_unprojectable_published_batch_does_not_block_other_sessions(
    pg_control_path, tmp_path, caplog
):
    from drover.server.control_outbox import (
        OutboxClaim,
        publish_outbox_batch,
        record_event_side_effects,
    )

    parquet = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet, duckdb_path=pg_control_path)
    stamp = datetime.now(timezone.utc) - timedelta(minutes=6)
    with control_plane_connection(pg_control_path) as control:
        control.execute(
            "INSERT INTO harness_events(event_id,session_id,event_type,normalized_type,normalized_source,created_at,dedup_key) VALUES('orphan','missing','user_input','user_input','codex',?,'orphan-key')",
            [stamp],
        )
        record_event_side_effects(
            control,
            event_id="orphan",
            session_id="missing",
            event_type="user_input",
            content_preview="orphan",
            payload_json='{"text":"orphan"}',
            created_at=stamp,
            seq=None,
        )
        control.execute(
            "INSERT INTO control_outbox_batches(batch_id,state,lease_owner,lease_until,member_count) VALUES('orphan-batch','claimed','fixture',?,1)",
            [stamp + timedelta(hours=1)],
        )
        control.execute(
            "INSERT INTO control_outbox_batch_events(batch_id,event_id,ordinal) VALUES('orphan-batch','orphan',0)"
        )
        control.execute(
            "UPDATE control_outbox_events SET state='claimed',batch_id='orphan-batch',lease_owner='fixture',lease_until=?",
            [stamp + timedelta(hours=1)],
        )
        publish_outbox_batch(
            control,
            OutboxClaim(
                "orphan-batch", ("orphan",), "fixture", stamp + timedelta(hours=1)
            ),
            parquet_dir=parquet,
            now=stamp,
        )
    ingest_file(source(tmp_path), parquet_dir=parquet, duckdb_path=pg_control_path)
    exporter = ControlOutboxExporter(
        control_path=pg_control_path,
        analytical_path=pg_control_path,
        parquet_dir=parquet,
        batch_size=1,
    )
    result = exporter.run_once()
    assert result["pending"] == 0 and result["published_unacknowledged"] == 1
    assert result["stalled_published_batches"] == 1
    assert "unacknowledged after 5 minutes" in caplog.text
    with control_plane_connection(pg_control_path) as control:
        assert control.execute(
            "SELECT state FROM control_outbox_events WHERE event_id='s2-event-0'"
        ).fetchone() == ("acknowledged",)
    # Restoring metadata repairs the existing batch; no rejected/skipped data loss.
    registry = HarnessRegistry(pg_control_path)
    registry.register_host(host_id="repair", display_name="repair", kind="test")
    registry.create_session(
        session_id="missing", host_id="repair", harness="codex", command="test"
    )
    exporter.run_once()
    with control_plane_connection(pg_control_path) as control:
        assert control.execute(
            "SELECT state FROM control_outbox_events WHERE event_id='orphan'"
        ).fetchone() == ("acknowledged",)
