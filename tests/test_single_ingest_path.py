"""S2 transaction, byte limits, retention, prune cost and rollback contracts."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.control_exporter import ControlOutboxExporter
from drover.server.control_outbox import (
    MAX_INPUT_BYTES,
    TARGET_BATCH_BYTES,
    _claim_rows,
    _export_json,
    _payload_prune_candidates,
    claim_outbox_batch,
    cut_batch_by_bytes,
    export_projection,
    prune_acknowledged_outbox,
    prune_verified_payloads,
)
from drover.server.db import control_plane_connection
from drover.server.ingest import ingest_file
from drover.server.legacy_outbox import replay_legacy


def source(
    tmp_path, *, count=1, content="a substantive user question", session="s2-session"
):
    path = tmp_path / "events.jsonl"
    events = [
        dict(
            id=f"s2-event-{i}",
            session_id=session,
            timestamp=f"2026-10-04T12:00:{i:02d}+00:00",
            agent_id="s2-host",
            event_type="user_message",
            message=dict(role="user", content=content),
            raw_data=dict(_repo_owner="arniesaha", _repo_name="drover"),
        )
        for i in range(count)
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


@pytest.mark.parametrize(
    "sizes,expected",
    [
        ([2 * 1024**2] * 5, 3),
        ([9 * 1024**2, 100], 1),
        ([100, 9 * 1024**2], 1),
    ],
)
def test_byte_cut(sizes, expected):
    items = [(str(i), n) for i, n in enumerate(sizes)]
    assert cut_batch_by_bytes(items) == [str(i) for i in range(expected)]


def test_row_over_hard_limit_fails_loudly_and_does_not_block_smaller_prefix():
    huge = ("huge-id", MAX_INPUT_BYTES)
    with pytest.raises(ValueError, match="huge-id.*MAX_INPUT_BYTES"):
        cut_batch_by_bytes([huge])
    assert cut_batch_by_bytes([("small", 100), huge]) == ["small"]


def test_claim_cuts_serialized_projection_and_exports_large_row_alone(
    pg_control_path, tmp_path
):
    from drover.server.control_outbox import (
        acknowledge_outbox_batch,
        publish_outbox_batch,
    )

    legacy = tmp_path / "legacy"
    path = source(tmp_path, content="x" * (9 * 1024**2))
    ingest_file(path, parquet_dir=legacy, duckdb_path=pg_control_path)
    path = source(tmp_path, count=2, content="short")
    # First id is already in the outbox; second is a short new event.
    ingest_file(path, parquet_dir=legacy, duckdb_path=pg_control_path)
    with control_plane_connection(pg_control_path) as con:
        claim = claim_outbox_batch(con, owner="byte-test")
        assert claim.event_ids == ("s2-event-0",)
        rows = _claim_rows(con, claim)
        size = len(_export_json([rows, export_projection(con, rows)]).encode())
        assert TARGET_BATCH_BYTES < size < MAX_INPUT_BYTES
        receipt = publish_outbox_batch(con, claim, parquet_dir=legacy)
        acknowledge_outbox_batch(con, receipt.batch_id)
        second = claim_outbox_batch(con, owner="byte-test")
        assert second.event_ids == ("s2-event-1",)


def test_summary_failure_rolls_back_event_dedup_payload_and_outbox(
    pg_control_path, tmp_path, monkeypatch
):
    from drover.server.ledger import JobLedger

    path = source(tmp_path)

    def fail(*args, **kwargs):
        assert kwargs["con"] is not None
        raise RuntimeError("injected summary failure")

    monkeypatch.setattr(JobLedger, "enqueue", fail)
    with pytest.raises(RuntimeError, match="summary failure"):
        ingest_file(path, parquet_dir=tmp_path / "legacy", duckdb_path=pg_control_path)
    with control_plane_connection(pg_control_path) as con:
        for table in (
            "harness_events",
            "harness_event_payloads",
            "control_outbox_events",
            "pipeline_jobs",
        ):
            assert con.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_outbox_retention_13_vs_15_days_and_exact_boundary(pg_control_path):
    now = datetime.now(timezone.utc)
    with control_plane_connection(pg_control_path) as con:
        for age in (13, 14, 15):
            event = f"retained-{age}"
            con.execute(
                """INSERT INTO control_outbox_events
                (event_id,state,acknowledged_at) VALUES (?, 'acknowledged', ?)""",
                [event, now - timedelta(days=age)],
            )
            con.execute(
                """INSERT INTO harness_event_archives
                (event_id,batch_id,payload_sha256,verified_at,payload_pruned_at)
                VALUES (?, 'archive', 'hash', ?, ?)""",
                [event, now, now],
            )
        # Old hot rows must retain the ack dependency needed by payload pruning.
        con.execute(
            "INSERT INTO control_outbox_events (event_id,state,acknowledged_at) VALUES ('hot','acknowledged',?)",
            [now - timedelta(days=14.5)],
        )
        assert prune_acknowledged_outbox(con, now=now, limit=1) == 1
        assert {
            r[0]
            for r in con.execute(
                "SELECT event_id FROM control_outbox_events"
            ).fetchall()
        } == {"retained-13", "retained-14", "hot"}
        assert prune_acknowledged_outbox(con, now=now, retention_days=21) == 0
        with pytest.raises(ValueError, match="at least 14"):
            prune_acknowledged_outbox(con, retention_days=13)


def test_replay_acknowledged_events_is_idempotent_and_since_is_inclusive(
    pg_control_path, tmp_path
):
    legacy = tmp_path / "legacy"
    bootstrap(parquet_dir=legacy, duckdb_path=pg_control_path)
    path = source(tmp_path, count=3)
    ingest_file(path, parquet_dir=legacy, duckdb_path=pg_control_path)
    exporter = ControlOutboxExporter(
        control_path=pg_control_path,
        analytical_path=pg_control_path,
        parquet_dir=legacy,
        batch_size=1,
    )
    exporter.run_once()
    with control_plane_connection(pg_control_path) as con:
        assert con.execute(
            "SELECT count(*) FROM control_outbox_events WHERE state='acknowledged'"
        ).fetchone() == (1,)
        stamp = datetime(2026, 10, 4, tzinfo=timezone.utc)
        con.execute("UPDATE control_outbox_events SET committed_at=?", [stamp])
    # Roll back into an empty destination, including the already acked delivery.
    rollback = tmp_path / "rollback"
    assert replay_legacy(pg_control_path, rollback, since=stamp.timestamp() + 1) == 0
    assert (
        replay_legacy(pg_control_path, rollback, since=stamp.timestamp(), limit=1) == 3
    )
    assert (
        replay_legacy(pg_control_path, rollback, since=stamp.timestamp(), limit=2) == 0
    )
    with duckdb.connect() as con:
        assert con.execute(
            "SELECT count(*),count(DISTINCT dedup_key) FROM read_parquet(?,hive_partitioning=true)",
            [str(rollback / "agent_events/**/*.parquet")],
        ).fetchone() == (3, 3)


def test_prune_bounded_windows_under_statement_timeout(
    pg_control_path, tmp_path, monkeypatch
):
    """100k events/1k sessions resembles the observed 78,985-payload store."""
    from contextlib import contextmanager

    from drover.server import db

    with control_plane_connection(pg_control_path) as con:
        con.execute(
            """INSERT INTO harness_sessions (session_id,host_id,harness,command,status)
            SELECT 'session-' || i, 'host', 'codex', 'test', 'completed'
            FROM generate_series(0,999) i"""
        )
        con.execute("""INSERT INTO harness_events(event_id,session_id,event_type,seq)
            SELECT 'event-' || lpad(i::text,6,'0'), 'session-' || (i/100)::text,
                   'assistant_output', i%100 FROM generate_series(0,99999) i""")
        con.execute(
            """INSERT INTO harness_event_payloads(event_id,payload_json,payload_sha256)
            SELECT event_id, '{}', 'hash' FROM harness_events"""
        )
        con.execute("ANALYZE harness_events")
        con.execute("ANALYZE harness_event_payloads")
    calls = []
    original = db.control_plane_connection

    class Tracking:
        def __init__(self, con):
            self.con = con

        def execute(self, sql, params=None):
            if "WITH candidates AS MATERIALIZED" in sql:
                calls.append((sql, params))
            return self.con.execute(sql, params)

    @contextmanager
    def bounded(path, **kwargs):
        with original(path, **kwargs) as con:
            con.execute("SET statement_timeout='200ms'")
            try:
                yield Tracking(con)
            finally:
                con.execute("RESET statement_timeout")

    monkeypatch.setattr(db, "control_plane_connection", bounded)
    cursors = []
    result = prune_verified_payloads(
        pg_control_path,
        resolver=None,
        limit=37,
        time_budget_seconds=0.05,
        cursor_callback=cursors.append,
    )
    assert result["protected_dependency"] > 37
    assert len(calls) >= 2
    assert all(params[-1] == 37 for _, params in calls)
    assert cursors == sorted(cursors)
    # The next budgeted pass advances instead of starting at a protected head.
    old_cursor = cursors[-1]
    prune_verified_payloads(
        pg_control_path,
        resolver=None,
        limit=37,
        time_budget_seconds=0.05,
        after=old_cursor,
        cursor_callback=cursors.append,
    )
    assert cursors[-1] > old_cursor
    with original(pg_control_path) as con:
        query, params = calls[0]
        explain = con.execute("EXPLAIN (ANALYZE, BUFFERS) " + query, params).fetchall()
        plan = "\n".join(r[0] for r in explain)
        (tmp_path / "prune-explain.txt").write_text(plan)
        assert "Index Only Scan using harness_events_session_progress" in plan
        assert "Seq Scan on harness_events" not in plan
        assert len(_payload_prune_candidates(con, limit=37, after="event-050000")) == 37


def test_legacy_watcher_end_to_end_latency(pg_control_path, tmp_path):
    from drover.collect.sources import write_events_jsonl
    from drover.models import AgentEvent
    from drover.server.watcher import IncomingWatcher

    legacy = tmp_path / "legacy"
    bootstrap(parquet_dir=legacy, duckdb_path=pg_control_path)
    incoming = tmp_path / "incoming"
    exporter = ControlOutboxExporter(
        control_path=pg_control_path,
        analytical_path=pg_control_path,
        parquet_dir=legacy,
    )
    watcher = IncomingWatcher(
        incoming_dir=incoming, parquet_dir=legacy, duckdb_path=pg_control_path
    )
    exporter.start(shutdown_event=threading.Event())
    watcher.start()
    latencies = []
    try:
        for i in range(3):
            event = json.loads(source(tmp_path, session=f"latency-{i}").read_text())
            event["id"] = f"latency-{i}"
            started = time.monotonic()
            write_events_jsonl(
                [AgentEvent.model_validate(event)],
                incoming,
                run_id=str(i),
                source_id="latency",
            )
            found = False
            while time.monotonic() - started < 15:
                paths = list(
                    (legacy / "agent_events").glob("date=*/agent_id=*/outbox-*.parquet")
                )
                if paths:
                    with duckdb.connect() as con:
                        found = (
                            con.execute(
                                "SELECT id FROM read_parquet(?,union_by_name=true) WHERE id=?",
                                [[str(p) for p in paths], event["id"]],
                            ).fetchone()
                            is not None
                        )
                if found:
                    break
                time.sleep(0.05)
            assert found, exporter.health()
            latencies.append(time.monotonic() - started)
        assert max(latencies) < 10
        print(f"legacy watcher parquet latency seconds: {latencies}")
    finally:
        watcher.stop()
        exporter.stop()


def test_summary_claim_waits_for_complete_session_export(pg_control_path, tmp_path):
    from drover.server.ledger import SUMMARIZE_SESSION, JobLedger

    legacy = tmp_path / "legacy"
    bootstrap(parquet_dir=legacy, duckdb_path=pg_control_path)
    ingest_file(
        source(tmp_path, count=3), parquet_dir=legacy, duckdb_path=pg_control_path
    )
    ledger = JobLedger(pg_control_path)
    assert not ledger.has_due(SUMMARIZE_SESSION)
    assert ledger.claim(SUMMARIZE_SESSION, worker_id="test") == []
    exporter = ControlOutboxExporter(
        control_path=pg_control_path,
        analytical_path=pg_control_path,
        parquet_dir=legacy,
        batch_size=1,
    )
    exporter.run_once()
    assert ledger.claim(SUMMARIZE_SESSION, worker_id="test") == []
    exporter.run_once()
    assert ledger.claim(SUMMARIZE_SESSION, worker_id="test") == []
    exporter.run_once()
    assert ledger.has_due(SUMMARIZE_SESSION)
    assert len(ledger.claim(SUMMARIZE_SESSION, worker_id="test")) == 1


def test_shared_harness_and_collector_normalization_dedup(pg_control_path, tmp_path):
    from drover.models import AgentEvent, Message
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.ingest import _row_from_event
    from drover.server.memory_identity import project_control_event

    stamp = datetime(2026, 10, 4, tzinfo=timezone.utc)
    registry = HarnessRegistry(pg_control_path)
    registry.register_host(host_id="host", display_name="host", kind="test")
    registry.create_session(
        host_id="host", harness="codex", command="test", session_id="shared"
    )
    registry.append_event(
        session_id="shared",
        event_id="harness",
        event_type="user_input",
        created_at=stamp,
        payload={"text": "shared content"},
        seq=1,
    )
    expected = _row_from_event(
        AgentEvent(
            id="different-trace-id",
            session_id="shared",
            agent_id="codex",
            event_type="user_input",
            timestamp=stamp,
            message=Message(role="user", content="shared content"),
            raw_data={},
        ),
        None,
    )
    with control_plane_connection(pg_control_path) as con:
        assert con.execute(
            "SELECT dedup_key FROM harness_events WHERE event_id='harness'"
        ).fetchone() == (expected["dedup_key"],)
        assert claim_outbox_batch(con, owner="shared").event_ids == ("harness",)
    registry.append_event(
        session_id="shared",
        event_id="harness-redelivery",
        event_type="user_input",
        created_at=stamp,
        payload={"text": "shared content"},
        seq=1,
    )
    with control_plane_connection(pg_control_path) as con:
        assert con.execute("SELECT count(*) FROM control_outbox_events").fetchone() == (
            1,
        )


def test_retention_config_is_operator_settable(tmp_path):
    from drover.config import load_config

    path = tmp_path / "config.toml"
    path.write_text("[control_store]\noutbox_retention_days=21\n")
    assert load_config(path).control_store.outbox_retention_days == 21
    path.write_text("[control_store]\noutbox_retention_days=13\n")
    with pytest.raises(ValueError, match="at least 14"):
        load_config(path)


def test_new_collector_event_keeps_existing_legacy_session_history(
    pg_control_path, tmp_path
):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from drover.models import AgentEvent, Message
    from drover.server.ingest import _row_from_event

    legacy = tmp_path / "legacy"
    bootstrap(parquet_dir=legacy, duckdb_path=pg_control_path)
    old = _row_from_event(
        AgentEvent(
            id="historical",
            session_id="s2-session",
            agent_id="s2-host",
            timestamp=datetime(2026, 10, 3, tzinfo=timezone.utc),
            event_type="user_message",
            message=Message(role="user", content="historical request"),
            raw_data={},
        ),
        None,
    )
    directory = legacy / "agent_events" / f"date={old['date']}" / "agent_id=s2-host"
    directory.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [{k: v for k, v in old.items() if k not in ("date", "agent_id")}]
        ),
        directory / "historic.parquet",
    )
    ingest_file(source(tmp_path), parquet_dir=legacy, duckdb_path=pg_control_path)
    ControlOutboxExporter(
        control_path=pg_control_path,
        analytical_path=pg_control_path,
        parquet_dir=legacy,
        batch_size=1,
    ).run_once()
    with duckdb.connect(str(pg_control_path)) as con:
        assert con.execute(
            "SELECT id FROM agent_events WHERE session_id='s2-session' ORDER BY id"
        ).fetchall() == [("historical",), ("s2-event-0",)]
        assert con.execute(
            "SELECT count(*) FROM agent_events WHERE date='2026-10-03'"
        ).fetchone() == (1,)
        assert con.execute(
            "SELECT count(*) FROM agent_events_for_date('2026-10-03')"
        ).fetchone() == (1,)


def test_released_control_delivery_key_still_dedupes_after_upgrade(pg_control_path):
    from drover.server.harness.identity import harness_event_identity
    from drover.server.harness.registry import HarnessRegistry

    stamp = datetime(2026, 10, 4, tzinfo=timezone.utc)
    payload = {"text": "released control event"}
    registry = HarnessRegistry(pg_control_path)
    registry.register_host(host_id="host", display_name="host", kind="test")
    registry.create_session(
        host_id="host", harness="codex", command="test", session_id="released"
    )
    old_key = harness_event_identity(
        session_id="released",
        seq=1,
        event_type="user_input",
        created_at=stamp,
        payload=payload,
    )
    with control_plane_connection(pg_control_path) as con:
        con.execute(
            """INSERT INTO harness_events (event_id,session_id,event_type,created_at,seq,dedup_key,payload_json)
            VALUES ('released-id','released','user_input',?,1,?,?)""",
            [stamp, old_key, json.dumps(payload)],
        )
    event = registry.append_event(
        session_id="released",
        event_id="new-delivery-id",
        seq=1,
        event_type="user_input",
        created_at=stamp,
        payload=payload,
    )
    assert event.event_id == "released-id"
    with control_plane_connection(pg_control_path) as con:
        assert con.execute("SELECT count(*) FROM harness_events").fetchone() == (1,)


def test_legacy_sink_refuses_corrupt_hot_payload_before_writing(
    pg_control_path, tmp_path
):
    legacy = tmp_path / "legacy"
    bootstrap(parquet_dir=legacy, duckdb_path=pg_control_path)
    ingest_file(source(tmp_path), parquet_dir=legacy, duckdb_path=pg_control_path)
    with control_plane_connection(pg_control_path) as con:
        con.execute("UPDATE harness_event_payloads SET payload_sha256='corrupt'")
    exporter = ControlOutboxExporter(
        control_path=pg_control_path,
        analytical_path=pg_control_path,
        parquet_dir=legacy,
        batch_size=1,
    )
    with pytest.raises(RuntimeError, match="payload hash mismatch.*s2-event-0"):
        exporter.run_once()
    assert not [p for p in legacy.rglob("*.parquet") if "_seed" not in str(p)]
