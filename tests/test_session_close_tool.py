"""Tests for drover_session_close enqueue semantics.

The summary job lives on the PostgreSQL job ledger (#480); ``status`` is the
ledger's enqueue outcome and ``job_status`` the ledger row's status after it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drover.schema import bootstrap
from drover.server.db import open_duckdb_connection
from drover.server.ledger import SUMMARIZE_SESSION, JobLedger
from drover.server.mcp.tools import drover_session_close


def _seed(tmp_path: Path) -> Path:
    # The path `pg_control_path` registers PostgreSQL for.
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    with open_duckdb_connection(duckdb_path) as con:
        con.execute("""INSERT INTO control_memory_events
            (id, session_id, timestamp, event_type, role, content, dedup_key)
            VALUES ('event', 's1', now(), 'user_input', 'user', 'work', 'event')""")
    return duckdb_path


@pytest.fixture
def version(monkeypatch: pytest.MonkeyPatch) -> dict:
    current = {"value": "v1"}
    monkeypatch.setattr(
        "drover.server.mcp.tools.source_version_for_session",
        lambda con, sid: current["value"],
    )
    return current


def test_close_queues_a_ledger_job(
    tmp_path: Path, pg_control_path: Path, version: dict
) -> None:
    db = _seed(tmp_path)
    out = drover_session_close(duckdb_path=db, session_id="s1")
    assert out == {"session_id": "s1", "status": "queued", "job_status": "pending"}

    job = JobLedger(db).latest(SUMMARIZE_SESSION, "s1")
    assert job is not None and job.source_version == "v1"


def test_close_is_idempotent_when_pending(
    tmp_path: Path, pg_control_path: Path, version: dict
) -> None:
    db = _seed(tmp_path)
    drover_session_close(duckdb_path=db, session_id="s1")
    out = drover_session_close(duckdb_path=db, session_id="s1")
    assert out["status"] == "already_queued"


def test_close_no_op_when_done(
    tmp_path: Path, pg_control_path: Path, version: dict
) -> None:
    db = _seed(tmp_path)
    drover_session_close(duckdb_path=db, session_id="s1")
    ledger = JobLedger(db)
    [job] = ledger.claim(SUMMARIZE_SESSION, worker_id="test")
    assert ledger.complete(job)

    out = drover_session_close(duckdb_path=db, session_id="s1")
    assert out == {
        "session_id": "s1",
        "status": "already_done",
        "job_status": "succeeded",
    }


def test_close_does_not_requeue_same_failed_generation(
    tmp_path: Path, pg_control_path: Path, version: dict
) -> None:
    db = _seed(tmp_path)
    drover_session_close(duckdb_path=db, session_id="s1")
    ledger = JobLedger(db)
    [job] = ledger.claim(SUMMARIZE_SESSION, worker_id="test")
    assert ledger.fail(job, "empty transcript", retryable=False) == "quarantined"

    out = drover_session_close(duckdb_path=db, session_id="s1")
    assert out == {
        "session_id": "s1",
        "status": "already_failed",
        "job_status": "quarantined",
    }


def test_close_requeues_only_a_changed_source_generation(
    tmp_path: Path, pg_control_path: Path, version: dict
) -> None:
    db = _seed(tmp_path)
    assert drover_session_close(duckdb_path=db, session_id="s1")["status"] == "queued"
    assert (
        drover_session_close(duckdb_path=db, session_id="s1")["status"]
        == "already_queued"
    )
    version["value"] = "v2"
    assert drover_session_close(duckdb_path=db, session_id="s1")["status"] == "requeued"
    assert JobLedger(db).latest(SUMMARIZE_SESSION, "s1").source_version == "v2"


def test_close_is_unavailable_without_postgres(tmp_path: Path, version: dict) -> None:
    """A DuckDB control store has no job ledger: say so, never raise."""
    db = _seed(tmp_path)
    out = drover_session_close(duckdb_path=db, session_id="s1")
    assert out == {"session_id": "s1", "status": "unavailable", "job_status": None}


def test_close_accepts_native_end_intent_before_collector_ingestion(
    tmp_path, pg_control_path
):
    db = _seed(tmp_path)
    assert (
        drover_session_close(duckdb_path=db, session_id="missing")["status"] == "queued"
    )
    assert JobLedger(db).latest(SUMMARIZE_SESSION, "missing") is not None
