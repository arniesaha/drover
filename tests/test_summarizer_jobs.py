"""State-machine tests for source-versioned summarizer jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb

from drover.schema import bootstrap
from drover.server.summarizer.jobs import (
    enqueue_summary_generation,
    finish_summary_failure,
    source_version_for_session,
)


def _bootstrapped(tmp_path: Path) -> tuple[duckdb.DuckDBPyConnection, Path]:
    duckdb_path = tmp_path / "jobs.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    return duckdb.connect(str(duckdb_path)), duckdb_path


def test_same_source_version_does_not_reset_attempt_budget(tmp_path: Path) -> None:
    con, _ = _bootstrapped(tmp_path)
    try:
        assert enqueue_summary_generation(con, "s1", "v1") is True
        con.execute(
            "UPDATE summarize_jobs SET status='dead_lettered', attempts=5 "
            "WHERE session_id='s1'"
        )

        assert enqueue_summary_generation(con, "s1", "v1") is False
        assert con.execute(
            "SELECT status, attempts FROM summarize_jobs WHERE session_id='s1'"
        ).fetchone() == ("dead_lettered", 5)
    finally:
        con.close()


def test_new_source_version_opens_fresh_generation(tmp_path: Path) -> None:
    con, _ = _bootstrapped(tmp_path)
    try:
        assert enqueue_summary_generation(con, "s1", "v1") is True
        con.execute(
            "UPDATE summarize_jobs SET status='dead_lettered', attempts=5 "
            "WHERE session_id='s1'"
        )

        assert enqueue_summary_generation(con, "s1", "v2") is True
        assert con.execute(
            "SELECT source_version,status,attempts FROM summarize_jobs "
            "WHERE session_id='s1'"
        ).fetchone() == ("v2", "pending", 0)
    finally:
        con.close()


def test_legacy_null_source_version_is_backfilled_without_reset(tmp_path: Path) -> None:
    con, _ = _bootstrapped(tmp_path)
    try:
        con.execute(
            "INSERT INTO summarize_jobs "
            "(session_id, status, attempts, source_version) "
            "VALUES ('s1', 'dead_lettered', 5, NULL)"
        )

        assert enqueue_summary_generation(con, "s1", "v1") is False
        assert con.execute(
            "SELECT source_version, status, attempts FROM summarize_jobs "
            "WHERE session_id='s1'"
        ).fetchone() == ("v1", "dead_lettered", 5)
    finally:
        con.close()


def test_failure_waits_with_capped_exponential_backoff(tmp_path: Path) -> None:
    con, _ = _bootstrapped(tmp_path)
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    try:
        assert enqueue_summary_generation(con, "s1", "v1") is True

        outcome = finish_summary_failure(
            con,
            "s1",
            "v1",
            "backend failed",
            now=now,
            jitter=lambda low, high: high,
        )

        assert outcome == "retry_wait"
        assert con.execute(
            "SELECT status, attempts, next_run_at FROM summarize_jobs "
            "WHERE session_id='s1'"
        ).fetchone() == ("retry_wait", 1, datetime(2026, 8, 6, 12, 1, 12))
    finally:
        con.close()


def test_fifth_failure_dead_letters_generation(tmp_path: Path) -> None:
    con, _ = _bootstrapped(tmp_path)
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    try:
        assert enqueue_summary_generation(con, "s1", "v1") is True
        con.execute(
            "UPDATE summarize_jobs SET status='running', attempts=4 "
            "WHERE session_id='s1'"
        )

        outcome = finish_summary_failure(
            con,
            "s1",
            "v1",
            "backend failed",
            now=now,
            jitter=lambda _low, _high: 0,
        )

        assert outcome == "dead_lettered"
        assert con.execute(
            "SELECT status, attempts, next_run_at, dead_lettered_at "
            "FROM summarize_jobs WHERE session_id='s1'"
        ).fetchone() == ("dead_lettered", 5, None, datetime(2026, 8, 6, 12, 0))
    finally:
        con.close()


def test_stale_failure_cannot_spend_new_generation_budget(tmp_path: Path) -> None:
    con, _ = _bootstrapped(tmp_path)
    try:
        assert enqueue_summary_generation(con, "s1", "v2") is True

        outcome = finish_summary_failure(
            con,
            "s1",
            "v1",
            "obsolete failure",
            now=datetime(2026, 8, 6, tzinfo=timezone.utc),
            jitter=lambda _low, _high: 0,
        )

        assert outcome == "stale"
        assert con.execute(
            "SELECT status, attempts, last_error FROM summarize_jobs "
            "WHERE session_id='s1'"
        ).fetchone() == ("pending", 0, None)
    finally:
        con.close()


def test_source_version_hashes_stable_facts_not_content(tmp_path: Path) -> None:
    con = duckdb.connect()
    try:
        con.execute("""CREATE TABLE agent_events (
                 id VARCHAR, session_id VARCHAR, timestamp TIMESTAMPTZ,
                 dedup_key VARCHAR, repo_owner VARCHAR, repo_name VARCHAR,
                 content VARCHAR
               )""")
        con.execute("""INSERT INTO agent_events VALUES
                 ('e1', 's1', '2026-08-06T12:00:00Z', 'k1', 'acme', 'app',
                  'original private message')""")
        before = source_version_for_session(con, "s1")
        con.execute(
            "UPDATE agent_events SET content='different private message' WHERE id='e1'"
        )
        after_content_change = source_version_for_session(con, "s1")
        con.execute("""INSERT INTO agent_events VALUES
                 ('e2', 's1', '2026-08-06T12:01:00Z', 'k2', 'acme', 'app',
                  'another message')""")
        after_new_event = source_version_for_session(con, "s1")

        assert before == after_content_change
        assert after_new_event != before
        assert len(before) == 64
    finally:
        con.close()


def test_bootstrap_adds_retry_columns_to_existing_table(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "legacy.duckdb"
    con = duckdb.connect(str(duckdb_path))
    con.execute(
        "CREATE TABLE summarize_jobs (session_id VARCHAR PRIMARY KEY, status VARCHAR, "
        "attempts INTEGER DEFAULT 0, last_error VARCHAR, enqueued_at TIMESTAMP, "
        "updated_at TIMESTAMP)"
    )
    con.close()

    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)

    con = duckdb.connect(str(duckdb_path))
    try:
        columns = {
            row[1]: row[4]
            for row in con.execute("PRAGMA table_info('summarize_jobs')").fetchall()
        }
        assert columns["source_version"] is None
        assert columns["max_attempts"] == "5"
        assert columns["next_run_at"] is None
        assert columns["dead_lettered_at"] is None
        assert columns["stream_publish_needed"] is not None
    finally:
        con.close()
