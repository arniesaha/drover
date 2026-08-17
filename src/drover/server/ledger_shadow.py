"""Best-effort shadow-writes into the durable pipeline ledger (AGE-44).

This wires the existing ingest/derive paths into :mod:`drover.server.ledger` so the
durable ledger (``pipeline_receipts`` / ``pipeline_jobs`` /
``pipeline_job_attempts`` / ``pipeline_artifacts``) is populated *alongside*
today's behaviour, without changing serving semantics yet. Serving still reads
the existing ``*_jobs`` and projection tables; this is the shadow slice (design
work-item #2, building on AGE-42).

Everything here is intentionally best-effort: a ledger failure must never break
ingest or a worker. Every entry point catches, logs, and returns a neutral value
so the authoritative path keeps running even if the ledger tables are missing
(e.g. an un-bootstrapped DB) or a write conflicts. The worker-side cutover that
*reads* from the ledger is follow-on work-item #3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

import duckdb

from drover.server.db import open_duckdb_connection
from drover.server.ledger import (
    JOB_PENDING,
    JOB_RETRY_WAIT,
    JOB_SUCCEEDED,
    JOB_TERMINAL_FAILED,
    ArtifactSpec,
    Job,
    Ledger,
    ReceiptResult,
)

log = logging.getLogger("drover.ledger_shadow")

DEFAULT_WORKER_ID = "drover"


# --------------------------------------------------------------------------- #
# Ledger job-kind ⇄ serving-table registry                                    #
# --------------------------------------------------------------------------- #
#
# The durable ledger (``pipeline_jobs``) is the system of record for job
# lifecycle; the per-kind ``*_jobs`` tables remain the fast single-writer claim
# queue that workers actually drain. This map is the bridge the cutover (AGE-45)
# uses to keep the two consistent during crash recovery and operator replay:
# given a ledger ``job_kind`` it names the serving table and its subject column.


@dataclass(frozen=True)
class ServingBinding:
    table: str
    key_column: str


SERVING_JOBS: Mapping[str, ServingBinding] = {
    "summarize_session": ServingBinding("summarize_jobs", "session_id"),
    "regenerate_project_brief": ServingBinding("brief_jobs", "project_key"),
    "embed_session": ServingBinding("embed_jobs", "session_id"),
    "embed_span": ServingBinding("span_embed_jobs", "span_id"),
}


# --------------------------------------------------------------------------- #
# Receipts (caller already holds a write connection)                          #
# --------------------------------------------------------------------------- #


def record_receipt(
    con: duckdb.DuckDBPyConnection,
    *,
    source_kind: str,
    source_key: str,
    source_version: Optional[str] = None,
    subject_kind: Optional[str] = None,
    subject_key: Optional[str] = None,
    payload_hash: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> Optional[ReceiptResult]:
    """Shadow-record a source unit on the caller's open connection.

    Returns the :class:`ReceiptResult` (so callers can count fresh vs duplicate
    units) or ``None`` if the ledger write failed. Re-recording the same
    ``(source_kind, source_key, source_version)`` triple is a no-op lookup, which
    is what makes duplicate ingestion a receipt no-op.
    """
    try:
        return Ledger(con).record_receipt(
            source_kind=source_kind,
            source_key=source_key,
            source_version=source_version,
            subject_kind=subject_kind,
            subject_key=subject_key,
            payload_hash=payload_hash,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001 — shadow writes never break ingest
        log.debug(
            "ledger shadow record_receipt failed for %s/%s",
            source_kind,
            source_key,
            exc_info=True,
        )
        return None


# --------------------------------------------------------------------------- #
# Jobs (workers manage their own short-lived connection)                      #
# --------------------------------------------------------------------------- #


def begin_attempt(
    duckdb_path: Path,
    *,
    job_kind: str,
    subject_key: str,
    subject_kind: Optional[str] = None,
    caused_by_receipt_id: Optional[str] = None,
    worker_id: str = DEFAULT_WORKER_ID,
) -> Optional[str]:
    """Open-or-reuse the logical job, normalise it to ``pending``, and lease it.

    Returns the leased ``job_id`` on success, or ``None`` if the shadow could not
    take a clean lease (in which case :func:`succeed`/:func:`retry` become
    no-ops). A reused job is advanced into a fresh leasable generation so retries
    append attempts and replays supersede the prior winner — mirroring how the
    live ``*_jobs`` row is re-claimed.
    """
    try:
        con = open_duckdb_connection(duckdb_path)
    except Exception:  # noqa: BLE001
        log.debug("ledger shadow connect failed for %s", duckdb_path, exc_info=True)
        return None
    try:
        ledger = Ledger(con)
        job = ledger.open_job(
            job_kind=job_kind,
            subject_key=subject_key,
            subject_kind=subject_kind,
            caused_by_receipt_id=caused_by_receipt_id,
        ).job
        job = _make_leasable(ledger, job, job_kind, subject_key, subject_kind)
        if job is None or job.status != JOB_PENDING:
            return None
        try:
            ledger.lease_job(job.job_id, worker_id=worker_id)
        except duckdb.ConstraintException as exc:
            # Not transient, and not survivable by retrying: the attempt row
            # cannot be written, so the job never records an attempt, never
            # advances attempt_count, and recomputes the same colliding
            # attempt number on every cycle. #143 caught one job doing this
            # 916 times in a single log, at WARNING with a traceback, still
            # firing across restarts and burying real errors.
            #
            # Parked rather than retried. Dead-lettered specifically, because
            # terminal_failed is a reusable status and the next cycle would
            # requeue this same row; from dead_lettered a fresh job row starts
            # instead, which is also what clears a poisoned unique-index entry
            # for the old job_id.
            log.warning(
                "ledger shadow parking %s/%s: its attempt could not be "
                "recorded (%s)",
                job_kind,
                subject_key,
                exc,
            )
            ledger.abandon_job(job.job_id, error_message=str(exc))
            ledger.dead_letter_job(job.job_id)
            return None
        return job.job_id
    except Exception:  # noqa: BLE001
        log.warning(
            "ledger shadow begin_attempt failed for %s/%s",
            job_kind,
            subject_key,
            exc_info=True,
        )
        return None
    finally:
        con.close()


def _make_leasable(
    ledger: Ledger,
    job: Job,
    job_kind: str,
    subject_key: str,
    subject_kind: Optional[str],
) -> Optional[Job]:
    """Advance a reused job into a fresh ``pending`` generation, ready to lease."""
    if job.status == JOB_PENDING:
        return job
    if job.status == JOB_RETRY_WAIT:
        # A retry: requeue makes the same logical job runnable so the next lease
        # appends another attempt rather than duplicating the job.
        return ledger.requeue_job(job.job_id)
    if job.status == JOB_SUCCEEDED:
        # A replay over an already-won subject: retire the prior winner and start
        # a fresh generation.
        ledger.supersede_job(job.job_id)
    elif job.status == JOB_TERMINAL_FAILED:
        ledger.dead_letter_job(job.job_id)
    else:
        # leased (likely a stale lease from a crash) — don't fight it in the
        # shadow; the reconciler (#3) owns lease recovery.
        return None
    return ledger.open_job(
        job_kind=job_kind,
        subject_key=subject_key,
        subject_kind=subject_kind,
    ).job


def succeed(
    duckdb_path: Path,
    job_id: Optional[str],
    *,
    artifact_kind: str,
    subject_key: Optional[str],
    storage_uri: Optional[str] = None,
    content_hash: Optional[str] = None,
    version_token: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    metrics: Optional[Mapping[str, Any]] = None,
) -> None:
    """Close the leased attempt as succeeded and emit artifact lineage."""
    if job_id is None:
        return
    try:
        con = open_duckdb_connection(duckdb_path)
    except Exception:  # noqa: BLE001
        log.debug("ledger shadow connect failed for %s", duckdb_path, exc_info=True)
        return
    try:
        Ledger(con).succeed_job(
            job_id,
            artifact=ArtifactSpec(
                artifact_kind=artifact_kind,
                subject_key=subject_key,
                storage_uri=storage_uri,
                content_hash=content_hash,
                version_token=version_token,
                metadata=metadata,
            ),
            metrics=metrics,
        )
    except Exception:  # noqa: BLE001
        log.warning("ledger shadow succeed failed for job %s", job_id, exc_info=True)
    finally:
        con.close()


def retry(
    duckdb_path: Path,
    job_id: Optional[str],
    *,
    error_message: str,
    error_category: Optional[str] = None,
    next_run_at: Optional[datetime] = None,
) -> None:
    """Close the leased attempt as retryable and park the job in ``retry_wait``."""
    if job_id is None:
        return
    try:
        con = open_duckdb_connection(duckdb_path)
    except Exception:  # noqa: BLE001
        log.debug("ledger shadow connect failed for %s", duckdb_path, exc_info=True)
        return
    try:
        Ledger(con).retry_job(
            job_id,
            error_category=error_category,
            error_message=error_message,
            next_run_at=next_run_at,
        )
    except Exception:  # noqa: BLE001
        log.warning("ledger shadow retry failed for job %s", job_id, exc_info=True)
    finally:
        con.close()


def fail_and_dead_letter(
    duckdb_path: Path,
    job_id: Optional[str],
    *,
    error_message: str,
    error_category: Optional[str] = None,
) -> None:
    """Close a leased attempt terminally and park its job in the dead letter."""
    if job_id is None:
        return
    try:
        con = open_duckdb_connection(duckdb_path)
    except Exception:  # noqa: BLE001
        log.debug("ledger shadow connect failed for %s", duckdb_path, exc_info=True)
        return
    try:
        ledger = Ledger(con)
        ledger.fail_job(
            job_id,
            error_category=error_category,
            error_message=error_message,
        )
        ledger.dead_letter_job(job_id)
    except Exception:  # noqa: BLE001
        log.warning(
            "ledger shadow fail_and_dead_letter failed for job %s",
            job_id,
            exc_info=True,
        )
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Crash recovery & operator replay (AGE-45 cutover)                            #
# --------------------------------------------------------------------------- #


def recover_runnable(
    duckdb_path: Path,
    *,
    job_kind: str,
    stale_before: Optional[datetime] = None,
) -> dict[str, Any]:
    """Reconcile crashed in-flight work for ``job_kind`` back to runnable.

    This is the shared crash-recovery step the workers call at startup instead of
    each one carrying its own ad hoc "reset stranded ``running`` rows" logic. It
    reconciles purely from DuckDB:

    * stale ledger leases (``pipeline_jobs.status='leased'``) are reclaimed — the
      crashed attempt is closed append-only and the job walked back to
      ``pending``;
    * the matching serving rows (``<table>.status='running'``) are reset to
      ``pending`` so the worker re-drains them.

    With ``stale_before=None`` (the single-process default) every in-flight job is
    treated as crashed, which is correct at process startup when no drain is
    running yet. Best-effort: any failure is logged and a neutral result returned
    so a missing/locked ledger never blocks a worker from starting.
    """
    binding = SERVING_JOBS.get(job_kind)
    result: dict[str, Any] = {
        "job_kind": job_kind,
        "serving_reset": 0,
        "leases_reclaimed": [],
    }
    try:
        con = open_duckdb_connection(duckdb_path)
    except Exception:  # noqa: BLE001
        log.debug("ledger shadow connect failed for %s", duckdb_path, exc_info=True)
        return result
    try:
        result["leases_reclaimed"] = Ledger(con).reclaim_stale_leases(
            job_kind=job_kind, stale_before=stale_before
        )
        if binding is not None:
            result["serving_reset"] = _reset_running_serving(
                con, binding, stale_before=stale_before
            )
    except Exception:  # noqa: BLE001
        log.warning(
            "ledger shadow recover_runnable failed for %s", job_kind, exc_info=True
        )
    finally:
        con.close()
    if result["leases_reclaimed"] or result["serving_reset"]:
        log.info(
            "recovered runnable %s: serving_reset=%d leases_reclaimed=%d",
            job_kind,
            result["serving_reset"],
            len(result["leases_reclaimed"]),
        )
    return result


def _reset_running_serving(
    con: duckdb.DuckDBPyConnection,
    binding: ServingBinding,
    *,
    stale_before: Optional[datetime] = None,
) -> int:
    """Reset ``running`` serving rows to ``pending``; return rows affected."""
    where = "status = 'running'"
    params: list[Any] = []
    if stale_before is not None:
        where += " AND updated_at < ?"
        params.append(stale_before)
    before = con.execute(
        f"SELECT count(*) FROM {binding.table} WHERE {where}", params
    ).fetchone()[0]
    if not before:
        return 0
    con.execute(
        f"UPDATE {binding.table} SET status='pending', updated_at=now() WHERE {where}",
        params,
    )
    return int(before)


def replay(
    duckdb_path: Path,
    *,
    job_kind: str,
    subject_key: str,
    apply: bool = True,
) -> dict[str, Any]:
    """Promote a finished job for ``(job_kind, subject_key)`` back to ``pending``.

    Walks the durable ledger job to a fresh ``pending`` generation
    (:meth:`Ledger.replay_job` — append-only lineage, prior winner superseded) and
    upserts the serving row to ``pending`` so the worker re-runs it. Because the
    serving tables are keyed by subject, the upsert can never create a duplicate
    serving row, and the regenerated artifact supersedes the prior current one.

    With ``apply=False`` nothing is mutated; the returned ``eligible`` flag and
    current ledger status let an operator preview the effect first.
    """
    binding = SERVING_JOBS.get(job_kind)
    result: dict[str, Any] = {
        "job_kind": job_kind,
        "subject_key": subject_key,
        "eligible": False,
        "ledger_status": None,
        "serving_reset": False,
        "applied": apply,
    }
    try:
        con = open_duckdb_connection(duckdb_path)
    except Exception:  # noqa: BLE001
        log.debug("ledger shadow connect failed for %s", duckdb_path, exc_info=True)
        return result
    try:
        ledger = Ledger(con)
        latest = ledger.latest_job(job_kind, subject_key)
        if latest is None:
            return result
        result["ledger_status"] = latest.status
        # Leased == in flight; refuse to race it (reconcile the lease instead).
        result["eligible"] = latest.status != "leased"
        if not apply or not result["eligible"]:
            return result
        new_job = ledger.replay_job(job_kind=job_kind, subject_key=subject_key)
        if new_job is None:
            result["eligible"] = False
            return result
        if binding is not None:
            result["serving_reset"] = _upsert_serving_pending(con, binding, subject_key)
    except Exception:  # noqa: BLE001
        log.warning(
            "ledger shadow replay failed for %s/%s",
            job_kind,
            subject_key,
            exc_info=True,
        )
    finally:
        con.close()
    return result


def _upsert_serving_pending(
    con: duckdb.DuckDBPyConnection, binding: ServingBinding, subject_key: str
) -> bool:
    """Upsert the serving row for ``subject_key`` to ``pending`` (no duplicate)."""
    con.execute(
        f"""
        INSERT INTO {binding.table} ({binding.key_column}, status, attempts)
        VALUES (?, 'pending', 0)
        ON CONFLICT ({binding.key_column})
        DO UPDATE SET status='pending', last_error=NULL, updated_at=now()
        """,
        [subject_key],
    )
    return True
