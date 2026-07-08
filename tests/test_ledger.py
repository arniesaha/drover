"""Tests for the durable pipeline ledger (AGE-42).

These exercise the receipt / job / attempt / artifact state machines and the
idempotency rules that make Redis loss survivable: duplicate source units are a
no-op, retries append attempts instead of duplicating logical jobs, and artifact
supersession keeps exactly one current version while preserving history.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.ledger import (
    ArtifactSpec,
    IllegalTransition,
    Ledger,
    assert_job_transition,
    assert_receipt_transition,
)

_FIXED_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def con(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    bootstrap(parquet_dir=tmp_path / "lake", duckdb_path=tmp_path / "drover.duckdb")
    connection = duckdb.connect(str(tmp_path / "drover.duckdb"))
    yield connection
    connection.close()


@pytest.fixture()
def ledger(con: duckdb.DuckDBPyConnection) -> Ledger:
    counter = itertools.count(1)
    return Ledger(
        con,
        id_factory=lambda: f"id-{next(counter):04d}",
        clock=lambda: _FIXED_NOW,
    )


def _count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# --------------------------------------------------------------------------- #
# Schema                                                                       #
# --------------------------------------------------------------------------- #


def test_bootstrap_creates_ledger_tables(con: duckdb.DuckDBPyConnection) -> None:
    tables = {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert {
        "pipeline_receipts",
        "pipeline_jobs",
        "pipeline_job_attempts",
        "pipeline_artifacts",
    } <= tables


# --------------------------------------------------------------------------- #
# Receipts: idempotency fence                                                  #
# --------------------------------------------------------------------------- #


def test_duplicate_source_unit_is_a_noop_lookup(
    ledger: Ledger, con: duckdb.DuckDBPyConnection
) -> None:
    first = ledger.record_receipt(
        source_kind="agent_event_batch",
        source_key="batch-1",
        subject_kind="session",
        subject_key="sess-1",
    )
    assert first.is_duplicate is False
    assert first.receipt.status == "observed"

    second = ledger.record_receipt(
        source_kind="agent_event_batch",
        source_key="batch-1",
        subject_kind="session",
        subject_key="sess-1",
    )
    assert second.is_duplicate is True
    assert second.receipt.receipt_id == first.receipt.receipt_id
    # The fence prevents a second durable row (and thus a second downstream job).
    assert _count(con, "pipeline_receipts") == 1


def test_source_version_discriminates_receipts(ledger: Ledger) -> None:
    v1 = ledger.record_receipt(
        source_kind="otlp_span", source_key="span-1", source_version="v1"
    )
    v2 = ledger.record_receipt(
        source_kind="otlp_span", source_key="span-1", source_version="v2"
    )
    assert v1.is_duplicate is False
    assert v2.is_duplicate is False
    assert v1.receipt.receipt_id != v2.receipt.receipt_id


def test_receipt_apply_and_illegal_transition(ledger: Ledger) -> None:
    rid = ledger.record_receipt(
        source_kind="session_close", source_key="sess-9"
    ).receipt.receipt_id
    applied = ledger.mark_receipt(rid, "applied")
    assert applied.status == "applied"
    # applied is terminal.
    with pytest.raises(IllegalTransition):
        ledger.mark_receipt(rid, "duplicate")


# --------------------------------------------------------------------------- #
# Jobs: one logical row, retries append attempts                              #
# --------------------------------------------------------------------------- #


def test_open_job_is_idempotent_per_subject(ledger: Ledger) -> None:
    a = ledger.open_job(job_kind="summarize_session", subject_key="sess-1")
    b = ledger.open_job(job_kind="summarize_session", subject_key="sess-1")
    assert a.created is True
    assert b.created is False
    assert a.job.job_id == b.job.job_id

    other = ledger.open_job(job_kind="summarize_session", subject_key="sess-2")
    assert other.created is True
    assert other.job.job_id != a.job.job_id


def test_retry_appends_attempt_not_duplicate_job(
    ledger: Ledger, con: duckdb.DuckDBPyConnection
) -> None:
    job = ledger.open_job(job_kind="embed_session", subject_key="sess-1").job

    attempt1 = ledger.lease_job(job.job_id, worker_id="w1")
    assert attempt1.attempt_no == 1
    ledger.retry_job(job.job_id, error_category="timeout", error_message="boom")

    # A retry_wait job is reused, not duplicated.
    reopened = ledger.open_job(job_kind="embed_session", subject_key="sess-1")
    assert reopened.created is False
    assert reopened.job.job_id == job.job_id

    ledger.requeue_job(job.job_id)
    attempt2 = ledger.lease_job(job.job_id, worker_id="w2")
    assert attempt2.attempt_no == 2

    assert _count(con, "pipeline_jobs") == 1
    assert _count(con, "pipeline_job_attempts") == 2
    # The first attempt's failure history is preserved (append-only).
    results = [
        row[0]
        for row in con.execute(
            "SELECT result FROM pipeline_job_attempts ORDER BY attempt_no"
        ).fetchall()
    ]
    assert results[0] == "retryable_failed"
    assert results[1] is None  # attempt 2 still running


def test_happy_path_success_with_artifact(
    ledger: Ledger, con: duckdb.DuckDBPyConnection
) -> None:
    job = ledger.open_job(job_kind="summarize_session", subject_key="sess-1").job
    ledger.lease_job(job.job_id, worker_id="w1")
    done = ledger.succeed_job(
        job.job_id,
        artifact=ArtifactSpec(
            artifact_kind="session_summary",
            subject_key="sess-1",
            storage_uri="duckdb://session_summaries/sess-1",
            content_hash="abc",
        ),
    )
    assert done.status == "succeeded"
    assert done.latest_artifact_id is not None

    attempt_result = con.execute(
        "SELECT result FROM pipeline_job_attempts WHERE job_id = ?", [job.job_id]
    ).fetchone()[0]
    assert attempt_result == "succeeded"

    current = con.execute(
        "SELECT count(*) FROM pipeline_artifacts WHERE is_current"
    ).fetchone()[0]
    assert current == 1


def test_terminal_failure_then_dead_letter(ledger: Ledger) -> None:
    job = ledger.open_job(job_kind="embed_span", subject_key="span-1").job
    ledger.lease_job(job.job_id, worker_id="w1")
    failed = ledger.fail_job(job.job_id, error_category="bad_input")
    assert failed.status == "terminal_failed"
    dead = ledger.dead_letter_job(job.job_id)
    assert dead.status == "dead_lettered"


def test_cancel_leased_closes_attempt(
    ledger: Ledger, con: duckdb.DuckDBPyConnection
) -> None:
    job = ledger.open_job(job_kind="embed_span", subject_key="span-2").job
    ledger.lease_job(job.job_id, worker_id="w1")
    cancelled = ledger.cancel_job(job.job_id)
    assert cancelled.status == "cancelled"
    result = con.execute(
        "SELECT result FROM pipeline_job_attempts WHERE job_id = ?", [job.job_id]
    ).fetchone()[0]
    assert result == "cancelled"


# --------------------------------------------------------------------------- #
# Illegal transitions                                                          #
# --------------------------------------------------------------------------- #


def test_cannot_succeed_without_leasing(ledger: Ledger) -> None:
    job = ledger.open_job(job_kind="summarize_session", subject_key="sess-x").job
    # pending -> succeeded is not a legal job transition (must lease first).
    with pytest.raises(IllegalTransition):
        ledger.succeed_job(job.job_id)


def test_cannot_lease_a_cancelled_job(ledger: Ledger) -> None:
    job = ledger.open_job(job_kind="summarize_session", subject_key="sess-y").job
    ledger.cancel_job(job.job_id)
    with pytest.raises(IllegalTransition):
        ledger.lease_job(job.job_id, worker_id="w1")


def test_cannot_dead_letter_a_pending_job(ledger: Ledger) -> None:
    job = ledger.open_job(job_kind="summarize_session", subject_key="sess-z").job
    with pytest.raises(IllegalTransition):
        ledger.dead_letter_job(job.job_id)


@pytest.mark.parametrize(
    "current,new",
    [
        ("pending", "leased"),
        ("leased", "succeeded"),
        ("leased", "retry_wait"),
        ("retry_wait", "pending"),
        ("leased", "terminal_failed"),
        ("terminal_failed", "dead_lettered"),
        ("succeeded", "superseded"),
        ("pending", "cancelled"),
    ],
)
def test_legal_job_transition_matrix(current: str, new: str) -> None:
    assert_job_transition(current, new)  # does not raise


@pytest.mark.parametrize(
    "current,new",
    [
        ("pending", "succeeded"),
        ("succeeded", "leased"),
        ("dead_lettered", "pending"),
        ("cancelled", "leased"),
        ("observed", "observed"),
    ],
)
def test_illegal_transition_matrix(current: str, new: str) -> None:
    with pytest.raises(IllegalTransition):
        if current in {"observed", "applied", "duplicate", "quarantined", "failed"}:
            assert_receipt_transition(current, new)
        else:
            assert_job_transition(current, new)


# --------------------------------------------------------------------------- #
# Artifacts: explicit supersession                                            #
# --------------------------------------------------------------------------- #


def test_artifact_supersession_keeps_one_current(
    ledger: Ledger, con: duckdb.DuckDBPyConnection
) -> None:
    # First generation: produce a session_summary, then supersede the job.
    gen1 = ledger.open_job(job_kind="summarize_session", subject_key="sess-1").job
    ledger.lease_job(gen1.job_id, worker_id="w1")
    ledger.succeed_job(
        gen1.job_id,
        artifact=ArtifactSpec(artifact_kind="session_summary", subject_key="sess-1"),
    )
    ledger.supersede_job(gen1.job_id)

    # Replay: a fresh generation re-summarizes the same session.
    gen2 = ledger.open_job(job_kind="summarize_session", subject_key="sess-1")
    assert gen2.created is True  # superseded job is not reused
    ledger.lease_job(gen2.job.job_id, worker_id="w2")
    ledger.succeed_job(
        gen2.job.job_id,
        artifact=ArtifactSpec(artifact_kind="session_summary", subject_key="sess-1"),
    )

    rows = con.execute(
        "SELECT is_current, supersedes_artifact_id FROM pipeline_artifacts "
        "WHERE subject_key = 'sess-1' ORDER BY created_at"
    ).fetchall()
    assert len(rows) == 2
    assert [r[0] for r in rows] == [False, True]  # only the newest is current
    assert rows[1][1] == _artifact_id_of(con, current=False)  # links to prior


def _artifact_id_of(con: duckdb.DuckDBPyConnection, *, current: bool) -> str:
    return con.execute(
        "SELECT artifact_id FROM pipeline_artifacts WHERE is_current = ?", [current]
    ).fetchone()[0]
