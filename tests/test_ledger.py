"""Contract tests for the PostgreSQL job ledger (#480)."""

from __future__ import annotations

import threading

import pytest

from drover.server.db import control_plane_connection
from drover.server.ledger import (
    BRIEF_PROJECT,
    EMBED_SESSION,
    SUMMARIZE_SESSION,
    JobLedger,
    MemoryStoreUnavailable,
    transaction,
)


def _ledger(path) -> JobLedger:
    return JobLedger(path, jitter=lambda a, b: 0.0)


def _make_due(path, job_id: str) -> None:
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE pipeline_jobs SET next_run_at = now() - interval '1 second' WHERE job_id = ?",
            [job_id],
        )


def _row(path, job_id: str) -> dict:
    with control_plane_connection(path) as con:
        cur = con.execute("SELECT * FROM pipeline_jobs WHERE job_id = ?", [job_id])
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, cur.fetchone()))


def test_requires_the_postgres_control_store(tmp_path):
    with pytest.raises(MemoryStoreUnavailable, match="postgres"):
        JobLedger(tmp_path / "drover.duckdb")


def test_migration_records_version_five(pg_control_path):
    with control_plane_connection(pg_control_path) as con:
        versions = [r[0] for r in con.execute(
            "SELECT version FROM control_schema_migrations ORDER BY version"
        ).fetchall()]
        legacy = con.execute("SELECT to_regclass('live_recap_jobs')").fetchone()[0]
    assert 5 in versions
    assert legacy is None


def test_enqueue_is_idempotent_per_source_version(pg_control_path):
    ledger = _ledger(pg_control_path)
    assert ledger.enqueue(SUMMARIZE_SESSION, "s1", source_version="v1") == "queued"
    assert ledger.enqueue(SUMMARIZE_SESSION, "s1", source_version="v1") == "already_queued"
    # A newer generation retargets the waiting job in place: still one live row.
    assert ledger.enqueue(SUMMARIZE_SESSION, "s1", source_version="v2") == "requeued"
    with control_plane_connection(pg_control_path) as con:
        rows = con.execute(
            "SELECT source_version, status FROM pipeline_jobs WHERE subject_key = 's1'"
        ).fetchall()
    assert rows == [("v2", "pending")]


def test_claim_complete_and_already_done(pg_control_path):
    ledger = _ledger(pg_control_path)
    ledger.enqueue(EMBED_SESSION, "s1", source_version="v1", payload={"a": 1})
    [job] = ledger.claim(EMBED_SESSION, worker_id="w")
    assert job.subject_key == "s1" and job.payload == {"a": 1} and job.attempt == 1
    assert ledger.claim(EMBED_SESSION, worker_id="w") == []
    assert ledger.complete(job) is True
    assert ledger.complete(job) is False  # the token is spent
    assert ledger.enqueue(EMBED_SESSION, "s1", source_version="v1") == "already_done"
    stats = ledger.stats()[EMBED_SESSION]
    assert stats["pending"] == 0 and stats["last_success_at"] is not None
    with control_plane_connection(pg_control_path) as con:
        attempts = con.execute(
            "SELECT attempt_no, result FROM pipeline_job_attempts WHERE job_id = ?",
            [job.job_id],
        ).fetchall()
    assert attempts == [(1, "succeeded")]


def test_retry_backoff_then_dead_letter_with_reason(pg_control_path):
    ledger = _ledger(pg_control_path)
    ledger.enqueue(BRIEF_PROJECT, "o/r")
    outcomes = []
    for _ in range(5):
        [job] = ledger.claim(BRIEF_PROJECT, worker_id="w")
        outcomes.append(ledger.fail(job, "backend 500", category="backend"))
        if outcomes[-1] == "retry_wait":
            row = _row(pg_control_path, job.job_id)
            assert row["next_run_at"] > row["updated_at"]
            assert ledger.claim(BRIEF_PROJECT, worker_id="w") == []  # not due yet
            _make_due(pg_control_path, job.job_id)
    assert outcomes == ["retry_wait"] * 4 + ["dead_lettered"]
    row = _row(pg_control_path, job.job_id)
    assert row["status"] == "dead_lettered"
    assert "exhausted 5/5 attempts" in row["disposition_reason"]
    assert ledger.stats()[BRIEF_PROJECT]["dead_lettered"] == 1
    # The same input earns nothing; a new one earns a fresh budget.
    assert ledger.enqueue(BRIEF_PROJECT, "o/r") == "already_failed"


def test_non_retryable_failure_quarantines_one_row_only(pg_control_path):
    ledger = _ledger(pg_control_path)
    ledger.enqueue(EMBED_SESSION, "poison")
    ledger.enqueue(EMBED_SESSION, "healthy")
    jobs = ledger.claim(EMBED_SESSION, worker_id="w", limit=2)
    by_subject = {j.subject_key: j for j in jobs}
    assert ledger.fail(by_subject["poison"], "bad input", retryable=False, category="validation") == "quarantined"
    assert ledger.complete(by_subject["healthy"])
    row = _row(pg_control_path, by_subject["poison"].job_id)
    assert row["status"] == "quarantined"
    assert row["disposition_reason"].startswith("quarantined (validation)")


def test_summary_streak_cap_suppresses_new_generations(pg_control_path):
    ledger = _ledger(pg_control_path)
    for version in ("v1", "v2", "v3"):
        ledger.enqueue(SUMMARIZE_SESSION, "s", source_version=version)
        [job] = ledger.claim(SUMMARIZE_SESSION, worker_id="w")
        assert ledger.fail(job, "nope", retryable=False) == "quarantined"
    assert ledger.enqueue(SUMMARIZE_SESSION, "s", source_version="v4") == "suppressed"
    assert ledger.enqueue(SUMMARIZE_SESSION, "s", source_version="v4", force=True) == "queued"


def test_running_job_is_superseded_and_its_completion_is_fenced(pg_control_path):
    ledger = _ledger(pg_control_path)
    ledger.enqueue(SUMMARIZE_SESSION, "s", source_version="v1")
    [old] = ledger.claim(SUMMARIZE_SESSION, worker_id="w")
    assert ledger.enqueue(SUMMARIZE_SESSION, "s", source_version="v2") == "requeued"
    assert ledger.complete(old) is False
    assert ledger.fail(old, "late") == "stale"
    row = _row(pg_control_path, old.job_id)
    assert row["status"] == "superseded" and "v2" in row["disposition_reason"]
    [new] = ledger.claim(SUMMARIZE_SESSION, worker_id="w")
    assert new.source_version == "v2"


def test_expired_lease_is_reclaimed_and_counts_as_failure(pg_control_path):
    ledger = _ledger(pg_control_path)
    ledger.enqueue(EMBED_SESSION, "s")
    [job] = ledger.claim(EMBED_SESSION, worker_id="dead-worker")
    with control_plane_connection(pg_control_path) as con:
        con.execute(
            "UPDATE pipeline_jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = ?",
            [job.job_id],
        )
    assert ledger.stats()[EMBED_SESSION]["expired_leases"] == 1
    assert ledger.reclaim_expired(EMBED_SESSION) == 1
    assert ledger.complete(job) is False  # the dead worker's token no longer matches
    row = _row(pg_control_path, job.job_id)
    assert row["status"] == "retry_wait" and row["failures"] == 1
    assert row["error_category"] == "lease_expired"


def test_release_does_not_spend_the_budget(pg_control_path):
    ledger = _ledger(pg_control_path)
    ledger.enqueue(EMBED_SESSION, "s")
    [job] = ledger.claim(EMBED_SESSION, worker_id="w")
    assert ledger.release(job, delay_seconds=0, reason="no embedder configured")
    [again] = ledger.claim(EMBED_SESSION, worker_id="w")
    assert again.failures == 0 and again.attempt == 2


def test_complete_joins_the_callers_transaction(pg_control_path):
    ledger = _ledger(pg_control_path)
    ledger.enqueue(SUMMARIZE_SESSION, "s", source_version="v1")
    [job] = ledger.claim(SUMMARIZE_SESSION, worker_id="w")
    with control_plane_connection(pg_control_path) as con:
        with pytest.raises(RuntimeError):
            with transaction(con):
                assert ledger.complete(job, con=con)
                ledger.enqueue(EMBED_SESSION, "s", source_version="v1", con=con)
                raise RuntimeError("derived write failed")
    assert _row(pg_control_path, job.job_id)["status"] == "running"
    assert ledger.stats()[EMBED_SESSION]["pending"] == 0


def test_concurrent_claims_never_share_a_job(pg_control_path):
    ledger = _ledger(pg_control_path)
    for i in range(20):
        ledger.enqueue(EMBED_SESSION, f"s{i}")
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while True:
            jobs = ledger.claim(EMBED_SESSION, worker_id=name, limit=3)
            if not jobs:
                return
            with lock:
                claimed.extend(j.subject_key for j in jobs)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(f"s{i}" for i in range(20))


def test_concurrent_enqueues_keep_one_live_row(pg_control_path):
    ledger = _ledger(pg_control_path)
    barrier = threading.Barrier(4)

    def enqueue(version: str) -> None:
        barrier.wait()
        ledger.enqueue(SUMMARIZE_SESSION, "s", source_version=version)

    threads = [threading.Thread(target=enqueue, args=(f"v{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with control_plane_connection(pg_control_path) as con:
        live = con.execute(
            "SELECT count(*) FROM pipeline_jobs WHERE subject_key = 's' "
            "AND status IN ('pending', 'running', 'retry_wait')"
        ).fetchone()[0]
    assert live == 1


def test_due_claim_uses_the_partial_index(pg_control_path):
    with control_plane_connection(pg_control_path) as con:
        con.execute("SET enable_seqscan = off")
        plan = "\n".join(
            r[0] for r in con.execute(
                """EXPLAIN SELECT job_id FROM pipeline_jobs
                    WHERE job_kind = 'embed_session' AND status IN ('pending', 'retry_wait')
                      AND next_run_at <= now()
                    ORDER BY priority DESC, next_run_at, enqueued_at
                    LIMIT 5 FOR UPDATE SKIP LOCKED"""
            ).fetchall()
        )
    assert "pipeline_jobs_due" in plan
