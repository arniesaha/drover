"""Source versions and summary-generation enqueue on the PostgreSQL ledger (#480).

The retry budget, backoff, supersession and streak cap themselves are the
ledger's (tests/test_ledger.py); these cover what the summarizer layers on
top: the source-version hash and the enqueue helper's contract.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from drover.schema import bootstrap
from drover.server.db import control_plane_connection
from drover.server.ledger import SUMMARIZE_SESSION, JobLedger
from drover.server.summarizer.jobs import (
    enqueue_summary_generation,
    source_version_for_session,
)


def _dead_letter(store_path: Path, session_id: str, version: str) -> None:
    """Drive one generation through its whole budget to dead_lettered."""
    ledger = JobLedger(store_path, jitter=lambda _low, _high: 0)
    assert enqueue_summary_generation(store_path, session_id, version) in (
        "queued",
        "requeued",
    )
    while True:
        with control_plane_connection(store_path) as con:
            con.execute(
                "UPDATE pipeline_jobs SET next_run_at = now() "
                "WHERE subject_key = ? AND status = 'retry_wait'",
                [session_id],
            )
        (job,) = ledger.claim(SUMMARIZE_SESSION, worker_id="test")
        if ledger.fail(job, "backend failed") == "dead_lettered":
            return


def test_enqueue_opens_one_live_generation(pg_control_path: Path) -> None:
    assert enqueue_summary_generation(pg_control_path, "s1", "v1") == "queued"
    assert enqueue_summary_generation(pg_control_path, "s1", "v1") == "already_queued"
    assert enqueue_summary_generation(pg_control_path, "s1", "v2") == "requeued"

    job = JobLedger(pg_control_path).latest(SUMMARIZE_SESSION, "s1")
    assert (job.source_version, job.status, job.failures) == ("v2", "pending", 0)


def test_dead_lettered_version_earns_nothing_but_a_new_one_reruns(
    pg_control_path: Path,
) -> None:
    _dead_letter(pg_control_path, "s1", "v1")

    assert enqueue_summary_generation(pg_control_path, "s1", "v1") == "already_failed"
    assert enqueue_summary_generation(pg_control_path, "s1", "v2") == "queued"


def test_repeated_dead_letters_stop_opening_new_generations(
    pg_control_path: Path,
) -> None:
    for version in ("v1", "v2", "v3"):
        _dead_letter(pg_control_path, "s1", version)

    assert enqueue_summary_generation(pg_control_path, "s1", "v4") == "suppressed"


def test_enqueue_without_postgres_is_an_unavailable_no_op(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)

    assert enqueue_summary_generation(duckdb_path, "s1", "v1") == "unavailable"


def test_source_version_hashes_stable_facts_not_content(tmp_path: Path) -> None:
    con = duckdb.connect()
    try:
        con.execute("""CREATE TABLE agent_events (
                 id VARCHAR, session_id VARCHAR, timestamp TIMESTAMPTZ,
                 dedup_key VARCHAR, repo_owner VARCHAR, repo_name VARCHAR,
                 content VARCHAR, role VARCHAR, event_type VARCHAR, raw_data VARCHAR
               )""")
        con.execute("""INSERT INTO agent_events VALUES
                 ('e1', 's1', '2026-08-06T12:00:00Z', 'k1', 'acme', 'app',
                  'original private message', 'user', 'user_message', '{}')""")
        before = source_version_for_session(con, "s1")
        con.execute(
            "UPDATE agent_events SET content='different private message' WHERE id='e1'"
        )
        after_content_change = source_version_for_session(con, "s1")
        con.execute("""INSERT INTO agent_events VALUES
                 ('e2', 's1', '2026-08-06T12:01:00Z', 'k2', 'acme', 'app',
                  'another message', 'assistant', 'assistant_message', '{}')""")
        after_new_event = source_version_for_session(con, "s1")

        assert before == after_content_change
        assert after_new_event != before
        assert len(before) == 64
    finally:
        con.close()
