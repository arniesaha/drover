"""Tests for drover_session_close enqueue semantics."""

from __future__ import annotations

from pathlib import Path

import duckdb

from drover.schema import bootstrap
from drover.server.mcp.tools import drover_session_close


def _seed(tmp_path: Path) -> Path:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return duckdb_path


def _job_row(duckdb_path: Path, session_id: str) -> tuple | None:
    con = duckdb.connect(str(duckdb_path))
    try:
        return con.execute(
            "SELECT session_id, status, attempts, last_error FROM summarize_jobs WHERE session_id=?",
            [session_id],
        ).fetchone()
    finally:
        con.close()


def test_close_inserts_pending_job(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    out = drover_session_close(duckdb_path=db, session_id="s1")
    assert out == {"session_id": "s1", "status": "queued"}

    row = _job_row(db, "s1")
    assert row[0] == "s1" and row[1] == "pending"


def test_close_is_idempotent_when_pending(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    drover_session_close(duckdb_path=db, session_id="s1")
    out = drover_session_close(duckdb_path=db, session_id="s1")
    assert out["status"] == "already_queued"


def test_close_no_op_when_done(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    drover_session_close(duckdb_path=db, session_id="s1")
    con = duckdb.connect(str(db))
    try:
        con.execute("UPDATE summarize_jobs SET status='done' WHERE session_id='s1'")
    finally:
        con.close()
    out = drover_session_close(duckdb_path=db, session_id="s1")
    assert out["status"] == "already_done"


def test_close_requeues_errored_jobs(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    drover_session_close(duckdb_path=db, session_id="s1")
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "UPDATE summarize_jobs SET status='errored', last_error='oops' WHERE session_id='s1'"
        )
    finally:
        con.close()
    out = drover_session_close(duckdb_path=db, session_id="s1")
    assert out["status"] == "requeued"

    row = _job_row(db, "s1")
    assert row[1] == "pending"
    assert row[3] is None
