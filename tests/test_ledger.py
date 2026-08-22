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


# --------------------------------------------------------------------------- #
# A job that cannot open an attempt (#143)                                     #
# --------------------------------------------------------------------------- #


def test_lease_numbers_the_attempt_from_history_not_the_counter(
    ledger: Ledger, con: duckdb.DuckDBPyConnection
) -> None:
    """``attempt_count`` is a denormalisation, and it is the field that drifts.

    In #143 a job sat at ``attempt_count = 0`` with an attempt already recorded
    against it, so every lease recomputed ``attempt_no = 1`` and collided with
    the unique key forever. The attempts table is the source of truth for how
    many attempts a job has had, so the next number comes from there.
    """

    job = ledger.open_job(job_kind="regenerate_project_brief", subject_key="repo").job
    ledger.lease_job(job.job_id, worker_id="w1")
    ledger.retry_job(job.job_id, error_message="transient")
    ledger.requeue_job(job.job_id)
    # Drift the counter back, exactly the shape the issue observed.
    con.execute(
        "UPDATE pipeline_jobs SET attempt_count = 0 WHERE job_id = ?", [job.job_id]
    )

    attempt = ledger.lease_job(job.job_id, worker_id="w2")

    assert attempt.attempt_no == 2, "a second attempt must not reuse attempt_no 1"


def test_a_lease_that_cannot_record_its_attempt_leaves_no_half_state(
    ledger: Ledger, con: duckdb.DuckDBPyConnection, monkeypatch
) -> None:
    """The insert and the status update are one unit or the job desynchronises.

    A failure between them is how a job ends up ``pending`` with an attempt row
    it does not count, which is the state that retries forever.
    """

    job = ledger.open_job(job_kind="summarize_session", subject_key="s1").job

    def _boom(*args, **kwargs):
        raise RuntimeError("status write failed")

    monkeypatch.setattr(ledger, "_set_job_status", _boom)

    with pytest.raises(RuntimeError):
        ledger.lease_job(job.job_id, worker_id="w1")

    rows = con.execute(
        "SELECT count(*) FROM pipeline_job_attempts WHERE job_id = ?", [job.job_id]
    ).fetchone()[0]
    assert rows == 0, "the attempt row must not survive a failed lease"
    assert ledger.latest_job("summarize_session", "s1").status == "pending"


def test_abandoning_a_pending_job_parks_it_for_good(ledger: Ledger) -> None:
    """A job that cannot be leased at all has to leave the reusable states.

    ``terminal_failed`` is still reusable, so parking there would let the next
    cycle requeue the same poisoned row and resume the loop. Dead-lettering is
    what actually ends it, and lets a fresh job row start instead.
    """

    job = ledger.open_job(job_kind="regenerate_project_brief", subject_key="repo").job

    ledger.abandon_job(job.job_id, error_message="could not open an attempt")
    parked = ledger.dead_letter_job(job.job_id)

    assert parked.status == "dead_lettered"


def test_the_database_fences_a_duplicate_the_read_before_write_missed(
    con: duckdb.DuckDBPyConnection, ledger: Ledger
) -> None:
    """The constraint must fence, not just `_find_receipt`.

    `record_receipt` reads before it writes, so two concurrent ingests can both
    miss and both insert. Nothing underneath objected: the fence is
    ``UNIQUE (source_kind, source_key, source_version)`` and `source_version`
    was nullable, and SQL treats NULLs as distinct. That left the 99% of rows
    which never set a version unconstrained (drover#256).

    Insert underneath the API, which is what a racing ingest effectively does.
    """
    ledger.record_receipt(source_kind="agent_event", source_key="sess-1")

    with pytest.raises(duckdb.ConstraintException):
        con.execute("""
            INSERT INTO pipeline_receipts
              (receipt_id, source_kind, source_key, source_version, status)
            VALUES ('racing-insert', 'agent_event', 'sess-1', NULL, 'observed')
            """)


def test_an_absent_version_is_stored_as_a_sentinel_and_read_back_as_absent(
    con: duckdb.DuckDBPyConnection, ledger: Ledger
) -> None:
    """The sentinel is a storage detail; callers still pass and see None."""
    result = ledger.record_receipt(source_kind="agent_event", source_key="sess-2")

    assert result.receipt.source_version is None
    stored = con.execute(
        "SELECT source_version FROM pipeline_receipts WHERE source_key = 'sess-2'"
    ).fetchone()[0]
    assert stored == ""

    again = ledger.record_receipt(source_kind="agent_event", source_key="sess-2")
    assert again.is_duplicate is True
    assert again.receipt.receipt_id == result.receipt.receipt_id


def test_a_version_that_is_set_still_discriminates(ledger: Ledger) -> None:
    """Distinct versions of one source key remain distinct source units."""
    first = ledger.record_receipt(
        source_kind="advisory_target_snapshot",
        source_key="target-1",
        source_version="v1",
    )
    second = ledger.record_receipt(
        source_kind="advisory_target_snapshot",
        source_key="target-1",
        source_version="v2",
    )

    assert second.is_duplicate is False
    assert second.receipt.receipt_id != first.receipt.receipt_id
    assert second.receipt.source_version == "v2"
