"""Bounded span analytics projection tests."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from drover import schema
from drover.schema import bootstrap
from drover.server.otlp.ingest import _coerce_to_arrow


def test_bootstrap_creates_additive_span_projection_tables(tmp_path):
    """A clean store has the empty relations needed for bounded projection."""
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)

    with duckdb.connect(str(duckdb_path)) as con:
        tables = {
            str(row[0])
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }

    assert {
        "analytics_span_partition_totals",
        "analytics_span_sessions",
        "analytics_span_partition_watermarks",
        "analytics_span_projection_state",
    } <= tables
    with duckdb.connect(str(duckdb_path)) as con:
        assert con.execute("""
            SELECT inventory_complete
            FROM analytics_span_projection_state
            WHERE projection_name = 'span_analytics'
            """).fetchone() == (True,)


def test_bootstrap_discovers_existing_span_dates_without_global_span_scan(
    tmp_path, monkeypatch
):
    """Bootstrap inventories filenames even when the global spans reader is unavailable."""
    parquet_dir = tmp_path / "parquet"
    part_dir = parquet_dir / "spans" / "date=2026-01-01"
    part_dir.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "start_time": pa.array([], type=pa.timestamp("us", tz="UTC")),
                "end_time": pa.array([], type=pa.timestamp("us", tz="UTC")),
            }
        ),
        part_dir / "part-000.parquet",
    )
    duckdb_path = tmp_path / "drover.duckdb"

    def fail_global_scan(_con):
        pytest.fail("bootstrap must not scan the global spans view")

    monkeypatch.setattr(
        schema, "_refresh_span_partition_activity", fail_global_scan, raising=False
    )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    with duckdb.connect(str(duckdb_path)) as con:
        assert con.execute("SELECT date FROM span_partition_activity").fetchall() == [
            ("2026-01-01",)
        ]
        direct_ingest_activity = datetime(2026, 1, 3, tzinfo=timezone.utc)
        con.execute(
            "UPDATE span_partition_activity SET latest_activity_at = ?",
            [direct_ingest_activity],
        )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    with duckdb.connect(str(duckdb_path)) as con:
        assert con.execute("""
            SELECT latest_activity_at
            FROM span_partition_activity
            WHERE date = '2026-01-01'
            """).fetchone() == (direct_ingest_activity,)


def _write_span_partition(
    parquet_dir,
    *,
    partition_date: str,
    session_id: str,
    token_count: int,
    repo_owner: str | None = "acme",
    repo_name: str | None = "alpha",
):
    started_at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    table = _coerce_to_arrow(
        [
            {
                "trace_id": f"trace-{session_id}",
                "span_id": f"span-{session_id}",
                "start_time": started_at,
                "end_time": started_at,
                "duration_ms": 25.0,
                "harness": "claude-code",
                "session_id": session_id,
                "agent_id": "agent-one",
                "llm_provider": "anthropic",
                "llm_model": "claude-test",
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "total_tokens": token_count,
                "cost_usd": 0.25,
                "cache_read_tokens": 4,
                "dedup_key": f"dedup-{session_id}",
            }
        ]
    )
    part_dir = parquet_dir / "spans" / f"date={partition_date}"
    part_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, part_dir / "part-000.parquet")


def _write_agent_event(
    parquet_dir,
    *,
    partition_date: str,
    session_id: str,
    repo_owner: str,
    repo_name: str,
):
    event_dir = (
        parquet_dir / "agent_events" / f"date={partition_date}" / "agent_id=agent-one"
    )
    event_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": [f"event-{session_id}"],
                "dedup_key": [f"event-dedup-{session_id}"],
                "session_id": [session_id],
                "agent_id": ["agent-one"],
                "timestamp": pa.array(
                    [datetime(2026, 1, 2, tzinfo=timezone.utc)],
                    type=pa.timestamp("us", tz="UTC"),
                ),
                "repo_owner": [repo_owner],
                "repo_name": [repo_name],
                "branch": ["main"],
                "raw_data": ["{}"],
            }
        ),
        event_dir / "part-000.parquet",
    )


def test_span_rollup_materializes_one_partition_into_compact_session_facts(tmp_path):
    """A pending span date becomes projection rows without a whole-store scan."""
    from drover.server.span_analytics_rollup import rollup_pending_span_analytics

    parquet_dir = tmp_path / "parquet"
    _write_span_partition(
        parquet_dir,
        partition_date="2026-01-01",
        session_id="long-session",
        token_count=42,
    )
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    report = rollup_pending_span_analytics(duckdb_path)

    assert (report.partitions, report.sessions) == (1, 1)
    with duckdb.connect(str(duckdb_path)) as con:
        assert (
            con.execute("""
            SELECT session_id, partition_date, project_key, total_tokens,
                   cache_read_tokens, has_tokens
            FROM analytics_span_partition_totals
            """).fetchall()
            == [("long-session", "2026-01-01", "acme/alpha", 42, 4, True)]
        )
        assert con.execute("""
            SELECT session_id, project_key, total_tokens
            FROM analytics_span_sessions
            """).fetchall() == [("long-session", "acme/alpha", 42)]
        assert con.execute("""
            SELECT partition_date
            FROM analytics_span_partition_watermarks
            """).fetchall() == [("2026-01-01",)]


def test_span_rollup_uses_explicit_cross_midnight_event_repo_when_span_lacks_one(
    tmp_path,
):
    """Adjacent canonical event evidence can fill only a span's missing repository."""
    from drover.server.span_analytics_rollup import rollup_pending_span_analytics

    parquet_dir = tmp_path / "parquet"
    _write_span_partition(
        parquet_dir,
        partition_date="2026-01-01",
        session_id="cross-midnight-session",
        token_count=12,
        repo_owner=None,
        repo_name=None,
    )
    _write_agent_event(
        parquet_dir,
        partition_date="2026-01-02",
        session_id="cross-midnight-session",
        repo_owner="acme",
        repo_name="beta",
    )
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    rollup_pending_span_analytics(duckdb_path)

    with duckdb.connect(str(duckdb_path)) as con:
        assert con.execute("""
            SELECT project_key
            FROM analytics_span_partition_totals
            WHERE session_id = 'cross-midnight-session'
            """).fetchone() == ("acme/beta",)


def test_span_rollup_does_not_advance_watermark_when_partition_write_fails(
    tmp_path, monkeypatch
):
    """A failed replace is transactionally replayable on the next worker pass."""
    from drover.server import span_analytics_rollup

    parquet_dir = tmp_path / "parquet"
    _write_span_partition(
        parquet_dir,
        partition_date="2026-01-01",
        session_id="atomic-session",
        token_count=19,
    )
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    def fail_rebuild(*_args, **_kwargs):
        raise RuntimeError("synthetic rebuild failure")

    monkeypatch.setattr(span_analytics_rollup, "_rebuild_sessions", fail_rebuild)
    with pytest.raises(RuntimeError, match="synthetic rebuild failure"):
        span_analytics_rollup.rollup_pending_span_analytics(duckdb_path)

    with duckdb.connect(str(duckdb_path)) as con:
        assert con.execute(
            "SELECT count(*) FROM analytics_span_partition_totals"
        ).fetchone() == (0,)
        assert con.execute(
            "SELECT count(*) FROM analytics_span_partition_watermarks"
        ).fetchone() == (0,)
