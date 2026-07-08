"""Tests for src/drover/server/rollup.py."""

from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.ingest import ingest_file
from drover.server.rollup import rollup_tasks

FIXTURE = Path(__file__).parent / "fixtures" / "incoming" / "sample_agent_events.jsonl"


@pytest.fixture
def tmp_lh(tmp_path):
    parquet_dir = tmp_path / "parquet"
    db_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    return parquet_dir, db_path


def test_rollup_sets_session_count_from_agent_events(tmp_lh):
    parquet_dir, db_path = tmp_lh
    ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)

    con = duckdb.connect(str(db_path))
    try:
        # ingest_file already calls rollup_tasks; verify counts are non-zero.
        rows = con.execute(
            "SELECT task_id, session_count FROM tasks WHERE task_id IS NOT NULL"
        ).fetchall()
        assert rows, "ingest should have created at least one task row"
        for _, n in rows:
            assert n >= 1, "session_count should be populated after rollup"
    finally:
        con.close()


def test_rollup_is_idempotent(tmp_lh):
    parquet_dir, db_path = tmp_lh
    ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)

    con = duckdb.connect(str(db_path))
    try:
        first = con.execute(
            "SELECT task_id, session_count, total_cost_usd FROM tasks ORDER BY task_id"
        ).fetchall()
        rollup_tasks(con)
        rollup_tasks(con)
        second = con.execute(
            "SELECT task_id, session_count, total_cost_usd FROM tasks ORDER BY task_id"
        ).fetchall()
        assert first == second
    finally:
        con.close()


def test_rollup_backfills_repo_fields_from_agent_events(tmp_lh):
    """If a task row was created before its events had attribution, rollup
    should fill in repo_owner / repo_name / branch from agent_events."""
    parquet_dir, db_path = tmp_lh
    ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)

    con = duckdb.connect(str(db_path))
    try:
        # Wipe repo fields on tasks; simulate the pre-attribution state.
        con.execute(
            "UPDATE tasks SET repo_owner = NULL, repo_name = NULL, branch = NULL"
        )
        # Ensure at least one agent_event has attribution so the rollup has
        # something to copy across.
        attributed = con.execute(
            "SELECT COUNT(*) FROM agent_events WHERE repo_owner IS NOT NULL"
        ).fetchone()[0]
        if not attributed:
            pytest.skip("fixture has no attributed agent_events")

        rollup_tasks(con)

        recovered = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE repo_owner IS NOT NULL"
        ).fetchone()[0]
        assert recovered >= 1
    finally:
        con.close()


def test_rollup_on_empty_lakehouse_returns_zero(tmp_lh):
    _, db_path = tmp_lh
    con = duckdb.connect(str(db_path))
    try:
        assert rollup_tasks(con) == 0
    finally:
        con.close()
