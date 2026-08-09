"""Source-versioned advisory jobs backed by the durable pipeline ledger."""

from __future__ import annotations

import math
from pathlib import Path
import time
from typing import Callable, Iterable

from drover.server.db import open_duckdb_connection
from drover.server.ledger import JOB_LEASED, JOB_PENDING, JOB_RETRY_WAIT, Job, Ledger

ADVISORY_JOB_KIND = "analyze_advisory_target"
ADVISORY_RECEIPT_KIND = "advisory_target_snapshot"
ADVISORY_ARTIFACT_KIND = "advisory_finding_batch"
DEFAULT_FULL_REVIEW_INTERVAL_SECONDS = 24 * 60 * 60.0
LIGHTWEIGHT_ANALYZER_IDS = (
    "deterministic.connector_freshness",
    "deterministic.provider_reset_windows",
    "deterministic.telemetry_coverage",
    "deterministic.cache_read_efficiency",
    "deterministic.routing_mismatch",
)


def _required(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def advisory_subject_key(analyzer_id: str, target_id: str) -> str:
    analyzer = _required(analyzer_id, "analyzer_id")
    target = _required(target_id, "target_id")
    if ":" in analyzer:
        raise ValueError("analyzer_id cannot contain ':'")
    return f"{analyzer}:{target}"


def enqueue_advisory_check(
    duckdb_path: str | Path,
    *,
    analyzer_id: str,
    target_id: str,
    source_version: str,
    force: bool = False,
    max_attempts: int = 5,
) -> Job:
    """Enqueue or reuse one analyzer/target/source-version check.

    The receipt is the source-version fence. The logical ledger subject remains
    exactly ``analyzer_id:target_id`` so retries append attempts to one job.
    ``force`` is reserved for an operator's Check Again action: it replays a
    completed generation while using the same current source-version receipt.
    """

    subject_key = advisory_subject_key(analyzer_id, target_id)
    version = _required(source_version, "source_version")
    con = open_duckdb_connection(Path(duckdb_path), role="worker")
    try:
        ledger = Ledger(con)
        receipt = ledger.record_receipt(
            source_kind=ADVISORY_RECEIPT_KIND,
            source_key=subject_key,
            source_version=version,
            subject_kind="advisory_target",
            subject_key=subject_key,
            payload_hash=version,
            metadata={"analyzer_id": analyzer_id, "target_id": target_id},
        )
        exact = _job_for_receipt(con, receipt.receipt.receipt_id)
        if receipt.is_duplicate and not force and exact is not None:
            return exact

        latest = ledger.latest_job(ADVISORY_JOB_KIND, subject_key)
        if latest is None:
            opened = ledger.open_job(
                job_kind=ADVISORY_JOB_KIND,
                subject_kind="advisory_target",
                subject_key=subject_key,
                caused_by_receipt_id=receipt.receipt.receipt_id,
                max_attempts=max_attempts,
            ).job
        elif force or latest.status not in {JOB_PENDING, JOB_LEASED, JOB_RETRY_WAIT}:
            replay = ledger.replay_job(
                job_kind=ADVISORY_JOB_KIND, subject_key=subject_key
            )
            if replay is None:
                return latest
            opened = replay.job
            con.execute(
                "UPDATE pipeline_jobs SET caused_by_receipt_id = ? WHERE job_id = ?",
                [receipt.receipt.receipt_id, opened.job_id],
            )
        else:
            opened = latest
            if latest.status != JOB_LEASED:
                con.execute(
                    "UPDATE pipeline_jobs SET caused_by_receipt_id = ? WHERE job_id = ?",
                    [receipt.receipt.receipt_id, latest.job_id],
                )
        return _load_job(con, opened.job_id)
    finally:
        con.close()


def enqueue_operational_checks(
    duckdb_path: str | Path,
    *,
    target_id: str,
    source_version: str,
    analyzer_ids: Iterable[str] = LIGHTWEIGHT_ANALYZER_IDS,
) -> list[Job]:
    """Enqueue lightweight analyzers after an operational snapshot changes."""

    return [
        enqueue_advisory_check(
            duckdb_path,
            analyzer_id=analyzer_id,
            target_id=target_id,
            source_version=source_version,
        )
        for analyzer_id in analyzer_ids
    ]


class AdvisoryScheduler:
    """Enqueue at most one deterministic full review per configured interval."""

    def __init__(
        self,
        *,
        duckdb_path: str | Path,
        analyzer_ids: Iterable[str],
        full_review_interval_seconds: float = DEFAULT_FULL_REVIEW_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        interval = float(full_review_interval_seconds)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("full review interval must be a positive number")
        self.duckdb_path = Path(duckdb_path)
        self.analyzer_ids = tuple(analyzer_ids)
        self.full_review_interval_seconds = interval
        self.clock = clock
        self._last_bucket: int | None = None

    def enqueue_due_full_review(self) -> list[Job]:
        bucket = math.floor(self.clock() / self.full_review_interval_seconds)
        if bucket == self._last_bucket:
            return []
        source_version = f"scheduled:{bucket}"
        jobs = [
            enqueue_advisory_check(
                self.duckdb_path,
                analyzer_id=analyzer_id,
                target_id="fleet",
                source_version=source_version,
            )
            for analyzer_id in self.analyzer_ids
        ]
        self._last_bucket = bucket
        return jobs


def _job_for_receipt(con, receipt_id: str) -> Job | None:
    row = con.execute(
        """
        SELECT job_id, job_kind, subject_key, status, attempt_count,
               max_attempts, latest_attempt_id, latest_artifact_id
        FROM pipeline_jobs WHERE caused_by_receipt_id = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        [receipt_id],
    ).fetchone()
    return Job(*row) if row is not None else None


def _load_job(con, job_id: str) -> Job:
    row = con.execute(
        """
        SELECT job_id, job_kind, subject_key, status, attempt_count,
               max_attempts, latest_attempt_id, latest_artifact_id
        FROM pipeline_jobs WHERE job_id = ?
        """,
        [job_id],
    ).fetchone()
    if row is None:
        raise KeyError(f"job {job_id!r} not found")
    return Job(*row)


__all__ = [
    "ADVISORY_ARTIFACT_KIND",
    "ADVISORY_JOB_KIND",
    "AdvisoryScheduler",
    "advisory_subject_key",
    "enqueue_advisory_check",
    "enqueue_operational_checks",
]
