"""Durable pipeline ledger helpers (AGE-31 / AGE-42).

This is the DuckDB-backed *system of record* for ingestion and derived-job
execution. It is the durable-truth counterpart to the Redis execution layer in
:mod:`drover.server.jobs`:

* **DuckDB (here) owns durable truth.** Receipts, logical jobs, attempt history,
  artifact lineage, idempotency, and final status all live in DuckDB so an
  operator can reconstruct what happened from the lakehouse alone, even after a
  crash or with Redis entirely absent.
* **Redis (``drover.server.jobs``) is optional execution coordination only.**
  Ready/delay queues, leases, and consumer-group fan-out can accelerate
  delivery, but losing Redis must never lose durable job state — it is a
  reconstructable cache, reconciled *from* this ledger.

The four tables live in :mod:`drover.schema` (``pipeline_receipts``,
``pipeline_jobs``, ``pipeline_job_attempts``, ``pipeline_artifacts``). This
module owns the *behaviour*: the legal state machines, idempotency rules, and
the small helper API the watcher/workers call.

Like :mod:`drover.server.jobs.streams`, this is written to double as executable
documentation: the transition maps below are the spec, and
:class:`IllegalTransition` is raised on any move not in them. It assumes the
existing single-process DuckDB queue model (claim-by-conditional-update); it
does not add cross-process locking.

State machines
--------------
Receipts::

    observed ──▶ applied        (source unit accepted, downstream effect committed)
    observed ──▶ duplicate      (same durable identity already applied)
    observed ──▶ quarantined    (payload malformed/unsafe; no job created)
    observed ──▶ failed         (unexpected persistence failure; safe to reconcile)
    failed   ──▶ observed|applied

Jobs::

    pending      ──▶ leased | cancelled
    leased       ──▶ succeeded | retry_wait | terminal_failed | cancelled
    retry_wait   ──▶ pending | cancelled
    succeeded    ──▶ superseded            (later replay produced a newer winner)
    terminal_failed ──▶ dead_lettered

Attempts are append-only: created on lease, closed in exactly one terminal
result (``succeeded``/``retryable_failed``/``terminal_failed``/``cancelled``/
``superseded``). A retry never mutates a logical job's identity — it appends a
new attempt row and advances the job snapshot.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

import duckdb

# --------------------------------------------------------------------------- #
# Status vocabularies and legal transitions                                   #
# --------------------------------------------------------------------------- #

# Receipts ------------------------------------------------------------------ #
RECEIPT_OBSERVED = "observed"
RECEIPT_APPLIED = "applied"
RECEIPT_DUPLICATE = "duplicate"
RECEIPT_QUARANTINED = "quarantined"
RECEIPT_FAILED = "failed"

RECEIPT_STATUSES = frozenset(
    {
        RECEIPT_OBSERVED,
        RECEIPT_APPLIED,
        RECEIPT_DUPLICATE,
        RECEIPT_QUARANTINED,
        RECEIPT_FAILED,
    }
)

RECEIPT_TRANSITIONS: dict[str, frozenset[str]] = {
    RECEIPT_OBSERVED: frozenset(
        {RECEIPT_APPLIED, RECEIPT_DUPLICATE, RECEIPT_QUARANTINED, RECEIPT_FAILED}
    ),
    # A failed receipt can be reconciled and retried.
    RECEIPT_FAILED: frozenset({RECEIPT_OBSERVED, RECEIPT_APPLIED}),
    # applied / duplicate / quarantined are terminal.
    RECEIPT_APPLIED: frozenset(),
    RECEIPT_DUPLICATE: frozenset(),
    RECEIPT_QUARANTINED: frozenset(),
}

# Jobs ---------------------------------------------------------------------- #
JOB_PENDING = "pending"
JOB_LEASED = "leased"
JOB_SUCCEEDED = "succeeded"
JOB_RETRY_WAIT = "retry_wait"
JOB_TERMINAL_FAILED = "terminal_failed"
JOB_DEAD_LETTERED = "dead_lettered"
JOB_CANCELLED = "cancelled"
JOB_SUPERSEDED = "superseded"

JOB_STATUSES = frozenset(
    {
        JOB_PENDING,
        JOB_LEASED,
        JOB_SUCCEEDED,
        JOB_RETRY_WAIT,
        JOB_TERMINAL_FAILED,
        JOB_DEAD_LETTERED,
        JOB_CANCELLED,
        JOB_SUPERSEDED,
    }
)

JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    # terminal_failed from pending is for a job that cannot open an attempt at
    # all (see Ledger.abandon_job). Without it such a job has no exit and is
    # re-leased forever, which is #143.
    JOB_PENDING: frozenset({JOB_LEASED, JOB_CANCELLED, JOB_TERMINAL_FAILED}),
    JOB_LEASED: frozenset(
        {JOB_SUCCEEDED, JOB_RETRY_WAIT, JOB_TERMINAL_FAILED, JOB_CANCELLED}
    ),
    JOB_RETRY_WAIT: frozenset({JOB_PENDING, JOB_CANCELLED}),
    JOB_SUCCEEDED: frozenset({JOB_SUPERSEDED}),
    JOB_TERMINAL_FAILED: frozenset({JOB_DEAD_LETTERED}),
    JOB_DEAD_LETTERED: frozenset(),
    JOB_CANCELLED: frozenset(),
    JOB_SUPERSEDED: frozenset(),
}

# A logical job is reused (not duplicated) by ``open_job`` while it is in one of
# these states; retries (retry_wait) and replays (succeeded) reuse the same row.
# A fully-parked job (dead_lettered/cancelled/superseded) lets a fresh
# generation start.
JOB_REUSABLE_STATUSES = frozenset(
    {JOB_PENDING, JOB_LEASED, JOB_RETRY_WAIT, JOB_SUCCEEDED, JOB_TERMINAL_FAILED}
)

# Attempts ------------------------------------------------------------------ #
ATTEMPT_SUCCEEDED = "succeeded"
ATTEMPT_RETRYABLE_FAILED = "retryable_failed"
ATTEMPT_TERMINAL_FAILED = "terminal_failed"
ATTEMPT_CANCELLED = "cancelled"
ATTEMPT_SUPERSEDED = "superseded"

ATTEMPT_RESULTS = frozenset(
    {
        ATTEMPT_SUCCEEDED,
        ATTEMPT_RETRYABLE_FAILED,
        ATTEMPT_TERMINAL_FAILED,
        ATTEMPT_CANCELLED,
        ATTEMPT_SUPERSEDED,
    }
)


class IllegalTransition(ValueError):
    """Raised when a receipt/job is moved through a transition not in the map."""


def assert_receipt_transition(current: str, new: str) -> None:
    _assert_transition("receipt", RECEIPT_TRANSITIONS, current, new)


def assert_job_transition(current: str, new: str) -> None:
    _assert_transition("job", JOB_TRANSITIONS, current, new)


def _assert_transition(
    label: str, table: Mapping[str, frozenset[str]], current: str, new: str
) -> None:
    allowed = table.get(current)
    if allowed is None:
        raise IllegalTransition(f"unknown {label} status {current!r}")
    if new not in allowed:
        raise IllegalTransition(
            f"illegal {label} transition {current!r} -> {new!r}; "
            f"allowed: {sorted(allowed) or '(terminal)'}"
        )


# --------------------------------------------------------------------------- #
# Lightweight row views                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    source_kind: str
    source_key: str
    source_version: Optional[str]
    subject_kind: Optional[str]
    subject_key: Optional[str]
    status: str


@dataclass(frozen=True)
class ReceiptResult:
    """Outcome of :meth:`Ledger.record_receipt`."""

    receipt: Receipt
    is_duplicate: bool


@dataclass(frozen=True)
class Job:
    job_id: str
    job_kind: str
    subject_key: str
    status: str
    attempt_count: int
    max_attempts: int
    latest_attempt_id: Optional[str]
    latest_artifact_id: Optional[str]


@dataclass(frozen=True)
class JobResult:
    """Outcome of :meth:`Ledger.open_job`."""

    job: Job
    created: bool


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    job_id: str
    attempt_no: int
    worker_id: Optional[str]
    result: Optional[str]


# --------------------------------------------------------------------------- #
# Ledger helper                                                               #
# --------------------------------------------------------------------------- #


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Ledger:
    """Helper API over the durable pipeline-ledger tables.

    Parameters
    ----------
    con:
        An open DuckDB connection whose schema has been bootstrapped.
    id_factory:
        Returns a unique id string. Overridable for deterministic tests.
    clock:
        Returns the current time. Overridable for deterministic tests.
    """

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        *,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._con = con
        self._new_id = id_factory
        self._now = clock

    # -- receipts ----------------------------------------------------------- #

    def record_receipt(
        self,
        *,
        source_kind: str,
        source_key: str,
        source_version: Optional[str] = None,
        subject_kind: Optional[str] = None,
        subject_key: Optional[str] = None,
        payload_hash: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ReceiptResult:
        """Idempotently record a source unit.

        The ``(source_kind, source_key, source_version)`` triple is the durable
        idempotency fence. If a row already exists for it, this is a *lookup*:
        the existing receipt is returned with ``is_duplicate=True`` and no new
        row (and therefore no new downstream job) is created. Otherwise a fresh
        ``observed`` receipt is inserted.
        """
        existing = self._find_receipt(source_kind, source_key, source_version)
        if existing is not None:
            return ReceiptResult(receipt=existing, is_duplicate=True)

        receipt_id = self._new_id()
        self._con.execute(
            """
            INSERT INTO pipeline_receipts
              (receipt_id, source_kind, source_key, source_version,
               subject_kind, subject_key, payload_hash, status,
               first_seen_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                receipt_id,
                source_kind,
                source_key,
                source_version,
                subject_kind,
                subject_key,
                payload_hash,
                RECEIPT_OBSERVED,
                self._now(),
                _dumps(metadata),
            ],
        )
        receipt = self._load_receipt(receipt_id)
        return ReceiptResult(receipt=receipt, is_duplicate=False)

    def mark_receipt(
        self, receipt_id: str, status: str, *, last_error: Optional[str] = None
    ) -> Receipt:
        """Move a receipt to ``status``, enforcing the receipt state machine."""
        current = self._load_receipt(receipt_id)
        assert_receipt_transition(current.status, status)
        applied_at = self._now() if status == RECEIPT_APPLIED else None
        self._con.execute(
            """
            UPDATE pipeline_receipts
               SET status = ?,
                   applied_at = COALESCE(?, applied_at),
                   last_error = COALESCE(?, last_error)
             WHERE receipt_id = ?
            """,
            [status, applied_at, last_error, receipt_id],
        )
        return self._load_receipt(receipt_id)

    # -- jobs --------------------------------------------------------------- #

    def open_job(
        self,
        *,
        job_kind: str,
        subject_key: str,
        subject_kind: Optional[str] = None,
        caused_by_receipt_id: Optional[str] = None,
        priority: int = 0,
        max_attempts: int = 5,
    ) -> JobResult:
        """Get-or-create the logical job for ``(job_kind, subject_key)``.

        A live job (see :data:`JOB_REUSABLE_STATUSES`) is reused so retries and
        replays never duplicate the logical row. A previously parked job
        (dead-lettered / cancelled / superseded) lets a fresh generation start.
        """
        existing = self._find_reusable_job(job_kind, subject_key)
        if existing is not None:
            return JobResult(job=existing, created=False)

        job_id = self._new_id()
        now = self._now()
        self._con.execute(
            """
            INSERT INTO pipeline_jobs
              (job_id, job_kind, subject_kind, subject_key, caused_by_receipt_id,
               status, priority, attempt_count, max_attempts,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            [
                job_id,
                job_kind,
                subject_kind,
                subject_key,
                caused_by_receipt_id,
                JOB_PENDING,
                priority,
                max_attempts,
                now,
                now,
            ],
        )
        return JobResult(job=self._load_job(job_id), created=True)

    def lease_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_expires_at: Optional[datetime] = None,
    ) -> Attempt:
        """Claim a ``pending`` job: open a new attempt and mark it ``leased``.

        Increments ``attempt_count`` and appends a ``pipeline_job_attempts`` row
        (append-only history). Returns the freshly-opened attempt.
        """
        job = self._load_job(job_id)
        assert_job_transition(job.status, JOB_LEASED)
        # Numbered from the attempts themselves, not from `attempt_count`.
        # The counter is a denormalisation and it is the field that drifts: in
        # #143 a job sat at attempt_count = 0 with history against it, so every
        # lease recomputed attempt_no = 1, collided with UNIQUE(job_id,
        # attempt_no), and retried forever without max_attempts ever engaging.
        attempt_no = int(
            self._con.execute(
                """
                SELECT COALESCE(MAX(attempt_no), 0) + 1
                  FROM pipeline_job_attempts
                 WHERE job_id = ?
                """,
                [job_id],
            ).fetchone()[0]
        )
        attempt_id = self._new_id()
        now = self._now()
        # One unit: an attempt row the job does not count is precisely the
        # half-state that cannot be leased again and cannot give up either.
        self._con.execute("BEGIN TRANSACTION")
        try:
            self._con.execute(
                """
                INSERT INTO pipeline_job_attempts
                  (attempt_id, job_id, attempt_no, worker_id, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [attempt_id, job_id, attempt_no, worker_id, now],
            )
            self._set_job_status(
                job_id,
                JOB_LEASED,
                attempt_count=attempt_no,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                latest_attempt_id=attempt_id,
            )
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        self._con.execute("COMMIT")
        return self._load_attempt(attempt_id)

    def abandon_job(
        self,
        job_id: str,
        *,
        error_category: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> Job:
        """Fail a job that could never open an attempt.

        ``fail_job`` closes the live attempt, and a job in this state has none
        to close: it is ``pending`` and every lease was rejected before an
        attempt row landed. Without a way out of ``pending`` other than
        ``leased``, such a job is retried for the life of the process (#143).

        The caller is expected to dead-letter it afterwards. ``terminal_failed``
        is still a reusable status, so parking there alone would let the next
        cycle requeue the same poisoned row and resume the loop.
        """

        job = self._load_job(job_id)
        assert_job_transition(job.status, JOB_TERMINAL_FAILED)
        del error_category, error_message  # recorded by the caller's log
        self._set_job_status(
            job_id, JOB_TERMINAL_FAILED, lease_owner=None, lease_expires_at=None
        )
        return self._load_job(job_id)

    def succeed_job(
        self,
        job_id: str,
        *,
        artifact: Optional["ArtifactSpec"] = None,
        metrics: Optional[Mapping[str, Any]] = None,
    ) -> Job:
        """Close the live attempt as succeeded and mark the job ``succeeded``.

        Optionally records the winning artifact (with explicit supersession of
        any prior current artifact for the same kind+subject).
        """
        job = self._load_job(job_id)
        assert_job_transition(job.status, JOB_SUCCEEDED)
        self._close_attempt(job.latest_attempt_id, ATTEMPT_SUCCEEDED, metrics=metrics)
        artifact_id = None
        if artifact is not None:
            artifact_id = self.record_artifact(
                job_id=job_id,
                attempt_id=job.latest_attempt_id,
                spec=artifact,
            )
        self._set_job_status(
            job_id,
            JOB_SUCCEEDED,
            succeeded_at=self._now(),
            lease_owner=None,
            lease_expires_at=None,
            latest_artifact_id=artifact_id,
        )
        return self._load_job(job_id)

    def retry_job(
        self,
        job_id: str,
        *,
        error_category: Optional[str] = None,
        error_message: Optional[str] = None,
        next_run_at: Optional[datetime] = None,
    ) -> Job:
        """Close the live attempt as retryable and park the job in ``retry_wait``."""
        job = self._load_job(job_id)
        assert_job_transition(job.status, JOB_RETRY_WAIT)
        self._close_attempt(
            job.latest_attempt_id,
            ATTEMPT_RETRYABLE_FAILED,
            error_category=error_category,
            error_message=error_message,
            retry_at=next_run_at,
        )
        self._set_job_status(
            job_id,
            JOB_RETRY_WAIT,
            next_run_at=next_run_at,
            lease_owner=None,
            lease_expires_at=None,
        )
        return self._load_job(job_id)

    def requeue_job(self, job_id: str) -> Job:
        """Reconciler step: make a ``retry_wait`` job runnable again (``pending``)."""
        job = self._load_job(job_id)
        assert_job_transition(job.status, JOB_PENDING)
        self._set_job_status(job_id, JOB_PENDING, next_run_at=None)
        return self._load_job(job_id)

    def reclaim_lease(
        self,
        job_id: str,
        *,
        error_message: str = "lease reclaimed during reconcile",
        error_category: str = "lease_reclaimed",
    ) -> Job:
        """Recover a crashed/stale ``leased`` job back to a runnable state.

        This is the durable crash-recovery primitive: a worker that dies while
        holding a lease leaves the job ``leased`` with an open attempt. Reconciling
        from DuckDB closes that attempt as ``retryable_failed`` (append-only — the
        crashed attempt is recorded, never dropped) and walks the job back to
        ``pending`` so the next worker re-runs it. Redis is never consulted.
        """
        job = self._load_job(job_id)
        if job.status != JOB_LEASED:
            raise IllegalTransition(
                f"reclaim_lease expects a leased job, got {job.status!r}"
            )
        self.retry_job(
            job_id, error_category=error_category, error_message=error_message
        )
        return self.requeue_job(job_id)

    def list_leased_jobs(
        self,
        *,
        job_kind: Optional[str] = None,
        stale_before: Optional[datetime] = None,
    ) -> list[Job]:
        """Return ``leased`` jobs, optionally scoped to a kind and a staleness cut.

        ``stale_before`` matches jobs whose ``lease_expires_at`` has passed, or —
        when no lease expiry was recorded — whose ``updated_at`` is older than the
        cut. With ``stale_before=None`` every leased job is returned (the
        single-process model treats any lease still held at reconcile time as
        crashed).
        """
        clauses = ["status = ?"]
        params: list[Any] = [JOB_LEASED]
        if job_kind is not None:
            clauses.append("job_kind = ?")
            params.append(job_kind)
        if stale_before is not None:
            clauses.append(
                "(lease_expires_at < ? OR "
                "(lease_expires_at IS NULL AND updated_at < ?))"
            )
            params.extend([stale_before, stale_before])
        rows = self._con.execute(
            f"""
            SELECT job_id FROM pipeline_jobs
             WHERE {' AND '.join(clauses)}
             ORDER BY updated_at ASC
            """,
            params,
        ).fetchall()
        return [self._load_job(r[0]) for r in rows]

    def reclaim_stale_leases(
        self,
        *,
        job_kind: Optional[str] = None,
        stale_before: Optional[datetime] = None,
    ) -> list[str]:
        """Reclaim every matching ``leased`` job; return the reclaimed job ids."""
        reclaimed: list[str] = []
        for job in self.list_leased_jobs(job_kind=job_kind, stale_before=stale_before):
            self.reclaim_lease(job.job_id)
            reclaimed.append(job.job_id)
        return reclaimed

    def latest_job(self, job_kind: str, subject_key: str) -> Optional[Job]:
        """Most-recent job snapshot for a subject, any status (read-only peek)."""
        return self._find_latest_job(job_kind, subject_key)

    def replay_job(
        self,
        *,
        job_kind: str,
        subject_key: str,
    ) -> Optional[JobResult]:
        """Promote the latest job for ``(job_kind, subject_key)`` back to ``pending``.

        This is the operator replay primitive. It never duplicates the logical job
        for a subject: a finished generation is first parked terminally
        (``succeeded`` → ``superseded``, ``terminal_failed`` → ``dead_lettered``)
        and a fresh generation is opened, so attempt history and artifact lineage
        stay append-only and the next success supersedes the prior current
        artifact rather than forking a parallel one.

        Returns the resulting :class:`JobResult`, or ``None`` when there is nothing
        to replay (no job yet) or the job is still ``leased`` (in flight — reconcile
        the lease first instead of racing it).
        """
        job = self._find_latest_job(job_kind, subject_key)
        if job is None:
            return None
        if job.status == JOB_LEASED:
            return None
        if job.status == JOB_PENDING:
            return JobResult(job=job, created=False)
        if job.status == JOB_RETRY_WAIT:
            return JobResult(job=self.requeue_job(job.job_id), created=False)
        if job.status == JOB_SUCCEEDED:
            self.supersede_job(job.job_id)
        elif job.status == JOB_TERMINAL_FAILED:
            self.dead_letter_job(job.job_id)
        # dead_lettered / cancelled / superseded (and the parked cases above) all
        # fall through to a fresh generation.
        return self.open_job(job_kind=job_kind, subject_key=subject_key)

    def fail_job(
        self,
        job_id: str,
        *,
        error_category: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> Job:
        """Close the live attempt as terminal and mark the job ``terminal_failed``."""
        job = self._load_job(job_id)
        assert_job_transition(job.status, JOB_TERMINAL_FAILED)
        self._close_attempt(
            job.latest_attempt_id,
            ATTEMPT_TERMINAL_FAILED,
            error_category=error_category,
            error_message=error_message,
        )
        self._set_job_status(
            job_id, JOB_TERMINAL_FAILED, lease_owner=None, lease_expires_at=None
        )
        return self._load_job(job_id)

    def dead_letter_job(self, job_id: str) -> Job:
        """Park a ``terminal_failed`` job in the dead-letter state."""
        job = self._load_job(job_id)
        assert_job_transition(job.status, JOB_DEAD_LETTERED)
        self._set_job_status(job_id, JOB_DEAD_LETTERED, dead_lettered_at=self._now())
        return self._load_job(job_id)

    def cancel_job(self, job_id: str) -> Job:
        """Operator stop for a ``pending``, ``retry_wait``, or ``leased`` job.

        Cancelling a ``leased`` job also closes its open attempt as ``cancelled``
        so attempt history stays append-only with exactly one terminal result.
        """
        job = self._load_job(job_id)
        assert_job_transition(job.status, JOB_CANCELLED)
        if job.status == JOB_LEASED:
            self._close_attempt(job.latest_attempt_id, ATTEMPT_CANCELLED)
        self._set_job_status(
            job_id, JOB_CANCELLED, lease_owner=None, lease_expires_at=None
        )
        return self._load_job(job_id)

    def supersede_job(self, job_id: str) -> Job:
        """Mark a ``succeeded`` job superseded by a newer winning generation."""
        job = self._load_job(job_id)
        assert_job_transition(job.status, JOB_SUPERSEDED)
        self._set_job_status(job_id, JOB_SUPERSEDED)
        return self._load_job(job_id)

    # -- artifacts ---------------------------------------------------------- #

    def record_artifact(
        self,
        *,
        job_id: str,
        attempt_id: Optional[str],
        spec: "ArtifactSpec",
    ) -> str:
        """Append a durable artifact row, superseding the prior current version.

        For singleton projections (one current ``session_summary`` per session,
        etc.) the previous current artifact with the same
        ``(artifact_kind, subject_key)`` is flipped to ``is_current = FALSE`` and
        linked via ``supersedes_artifact_id`` so history is preserved.
        """
        prior_id = None
        if spec.subject_key is not None:
            row = self._con.execute(
                """
                SELECT artifact_id FROM pipeline_artifacts
                 WHERE artifact_kind = ? AND subject_key = ? AND is_current
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                [spec.artifact_kind, spec.subject_key],
            ).fetchone()
            if row is not None:
                prior_id = row[0]
                self._con.execute(
                    "UPDATE pipeline_artifacts SET is_current = FALSE "
                    "WHERE artifact_id = ?",
                    [prior_id],
                )

        artifact_id = self._new_id()
        self._con.execute(
            """
            INSERT INTO pipeline_artifacts
              (artifact_id, job_id, attempt_id, artifact_kind, subject_key,
               storage_uri, content_hash, version_token, supersedes_artifact_id,
               is_current, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)
            """,
            [
                artifact_id,
                job_id,
                attempt_id,
                spec.artifact_kind,
                spec.subject_key,
                spec.storage_uri,
                spec.content_hash,
                spec.version_token,
                prior_id,
                self._now(),
                _dumps(spec.metadata),
            ],
        )
        return artifact_id

    # -- internal helpers --------------------------------------------------- #

    def _set_job_status(self, job_id: str, status: str, **fields: Any) -> None:
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, self._now()]
        for name, value in fields.items():
            sets.append(f"{name} = ?")
            params.append(value)
        params.append(job_id)
        self._con.execute(
            f"UPDATE pipeline_jobs SET {', '.join(sets)} WHERE job_id = ?", params
        )

    def _close_attempt(
        self,
        attempt_id: Optional[str],
        result: str,
        *,
        error_category: Optional[str] = None,
        error_message: Optional[str] = None,
        retry_at: Optional[datetime] = None,
        metrics: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if result not in ATTEMPT_RESULTS:
            raise IllegalTransition(f"unknown attempt result {result!r}")
        if attempt_id is None:
            raise IllegalTransition("no open attempt to close for this job")
        self._con.execute(
            """
            UPDATE pipeline_job_attempts
               SET result = ?, finished_at = ?, error_category = ?,
                   error_message = ?, retry_at = ?, metrics_json = ?
             WHERE attempt_id = ?
            """,
            [
                result,
                self._now(),
                error_category,
                error_message,
                retry_at,
                _dumps(metrics),
                attempt_id,
            ],
        )

    def _find_receipt(
        self, source_kind: str, source_key: str, source_version: Optional[str]
    ) -> Optional[Receipt]:
        # NULL source_version must match NULL (SQL ``= NULL`` never matches), so
        # branch on it explicitly.
        if source_version is None:
            row = self._con.execute(
                """
                SELECT receipt_id FROM pipeline_receipts
                 WHERE source_kind = ? AND source_key = ? AND source_version IS NULL
                """,
                [source_kind, source_key],
            ).fetchone()
        else:
            row = self._con.execute(
                """
                SELECT receipt_id FROM pipeline_receipts
                 WHERE source_kind = ? AND source_key = ? AND source_version = ?
                """,
                [source_kind, source_key, source_version],
            ).fetchone()
        return self._load_receipt(row[0]) if row is not None else None

    def _find_reusable_job(self, job_kind: str, subject_key: str) -> Optional[Job]:
        placeholders = ", ".join("?" for _ in JOB_REUSABLE_STATUSES)
        row = self._con.execute(
            f"""
            SELECT job_id FROM pipeline_jobs
             WHERE job_kind = ? AND subject_key = ? AND status IN ({placeholders})
             ORDER BY created_at DESC
             LIMIT 1
            """,
            [job_kind, subject_key, *sorted(JOB_REUSABLE_STATUSES)],
        ).fetchone()
        return self._load_job(row[0]) if row is not None else None

    def _find_latest_job(self, job_kind: str, subject_key: str) -> Optional[Job]:
        """Most-recent job for a subject regardless of status (for replay)."""
        row = self._con.execute(
            """
            SELECT job_id FROM pipeline_jobs
             WHERE job_kind = ? AND subject_key = ?
             ORDER BY created_at DESC
             LIMIT 1
            """,
            [job_kind, subject_key],
        ).fetchone()
        return self._load_job(row[0]) if row is not None else None

    def _load_receipt(self, receipt_id: str) -> Receipt:
        row = self._con.execute(
            """
            SELECT receipt_id, source_kind, source_key, source_version,
                   subject_kind, subject_key, status
              FROM pipeline_receipts WHERE receipt_id = ?
            """,
            [receipt_id],
        ).fetchone()
        if row is None:
            raise KeyError(f"receipt {receipt_id!r} not found")
        return Receipt(*row)

    def _load_job(self, job_id: str) -> Job:
        row = self._con.execute(
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

    def _load_attempt(self, attempt_id: str) -> Attempt:
        row = self._con.execute(
            """
            SELECT attempt_id, job_id, attempt_no, worker_id, result
              FROM pipeline_job_attempts WHERE attempt_id = ?
            """,
            [attempt_id],
        ).fetchone()
        if row is None:
            raise KeyError(f"attempt {attempt_id!r} not found")
        return Attempt(*row)


@dataclass(frozen=True)
class ArtifactSpec:
    """Description of a durable output to record via :meth:`Ledger.record_artifact`."""

    artifact_kind: str
    subject_key: Optional[str] = None
    storage_uri: Optional[str] = None
    content_hash: Optional[str] = None
    version_token: Optional[str] = None
    metadata: Optional[Mapping[str, Any]] = None


def _dumps(value: Optional[Mapping[str, Any]]) -> Optional[str]:
    return None if value is None else json.dumps(value, sort_keys=True)
