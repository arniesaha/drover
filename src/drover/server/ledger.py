"""The one authoritative job ledger for derived memory work (#464, #480).

Every derived-memory job -- session summaries, session embeddings, project
briefs and live recaps -- is a row in ``pipeline_jobs`` in the PostgreSQL
control store. There is no second queue: the legacy DuckDB ``*_jobs`` tables,
the DuckDB ledger shadow and the Redis delivery streams are gone from this
path, so there is nothing left to disagree with.

State machine::

    pending ──claim──▶ running ──complete──▶ succeeded
       ▲                  │
       │ (next_run_at)    ├──fail (retryable, budget left)──▶ retry_wait ──┐
       └──────────────────┼──────────────────────────────────────────────┘
                          ├──fail (budget spent / lease lost too often)──▶ dead_lettered
                          ├──fail (non-retryable input fault)───────────▶ quarantined
                          └──newer source generation / stale input──────▶ superseded

``succeeded``, ``dead_lettered``, ``quarantined`` and ``superseded`` are
terminal. Every terminal failure carries a ``disposition_reason``; the schema
refuses one without it.

Claims take due rows with ``FOR UPDATE SKIP LOCKED``, so two workers never
block on, or both take, one row, and one poisoned row cannot head the queue:
it is claimed, fails, and moves out of the due set like any other (#471).
Each claim mints a fresh lease token. ``complete``/``fail``/``heartbeat``
match on that token, so a worker whose lease expired and was reclaimed --
or whose generation was superseded -- cannot overwrite the newer owner.
Expired leases are reclaimed on every claim and count as a failure, so a job
that keeps killing its worker dead-letters instead of looping.

At most one *live* (pending/running/retry_wait) job exists per
``(job_kind, subject_key)``; a unique partial index enforces it. Enqueueing a
different ``source_version`` replaces a waiting job in place (it has not
started, so there is no history to keep) and supersedes a running one.
"""

from __future__ import annotations

import json
import random
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, Optional, Sequence

from drover.server.control_store import is_postgres_control_store
from drover.server.db import control_plane_connection

# --------------------------------------------------------------------------- #
# Vocabulary                                                                  #
# --------------------------------------------------------------------------- #

SUMMARIZE_SESSION = "summarize_session"
EMBED_SESSION = "embed_session"
BRIEF_PROJECT = "brief_project"
RECAP_SESSION = "recap_session"

JOB_KINDS = (SUMMARIZE_SESSION, EMBED_SESSION, BRIEF_PROJECT, RECAP_SESSION)

PENDING = "pending"
RUNNING = "running"
RETRY_WAIT = "retry_wait"
SUCCEEDED = "succeeded"
DEAD_LETTERED = "dead_lettered"
QUARANTINED = "quarantined"
SUPERSEDED = "superseded"

LIVE_STATUSES = (PENDING, RUNNING, RETRY_WAIT)
FAILED_STATUSES = (DEAD_LETTERED, QUARANTINED)
TERMINAL_STATUSES = (SUCCEEDED, DEAD_LETTERED, QUARANTINED, SUPERSEDED)
JOB_STATUSES = LIVE_STATUSES + TERMINAL_STATUSES

EnqueueOutcome = Literal[
    "queued",  # a new live job
    "requeued",  # replaced/superseded a live job for an older source version
    "already_queued",  # a live job for this exact source version exists
    "already_done",  # this exact source version already succeeded
    "already_failed",  # this exact source version already dead-lettered
    "suppressed",  # the subject hit its dead-letter streak cap
]
FailOutcome = Literal["retry_wait", "dead_lettered", "quarantined", "stale"]


@dataclass(frozen=True)
class JobPolicy:
    """Retry, lease and streak bounds for one job kind."""

    max_attempts: int = 5
    lease_seconds: int = 600
    retry_base_seconds: int = 60
    retry_max_seconds: int = 3600
    # Consecutive failed generations (since the last success) after which new
    # generations stop opening. A live session mints a generation on every
    # ingest, so a per-generation budget alone bounds nothing: one session
    # was once observed at 410 summary attempts.
    max_failed_streak: Optional[int] = None


POLICIES: Mapping[str, JobPolicy] = {
    SUMMARIZE_SESSION: JobPolicy(
        max_attempts=5, lease_seconds=900, max_failed_streak=3
    ),
    EMBED_SESSION: JobPolicy(max_attempts=5, lease_seconds=300),
    BRIEF_PROJECT: JobPolicy(max_attempts=5, lease_seconds=900),
    RECAP_SESSION: JobPolicy(max_attempts=8, lease_seconds=300),
}


class MemoryStoreUnavailable(RuntimeError):
    """Derived memory needs the PostgreSQL control store, and it is not configured."""


def require_memory_store(store_path: str | Path) -> None:
    if not is_postgres_control_store(store_path):
        raise MemoryStoreUnavailable(
            "derived memory (summaries, briefs, embeddings, recaps and their "
            "job ledger) requires control_store.backend = 'postgres'"
        )


def memory_store_available(store_path: str | Path) -> bool:
    return is_postgres_control_store(store_path)


@dataclass(frozen=True)
class ClaimedJob:
    """One leased job. Pass it back to ``complete``/``fail``/``heartbeat``."""

    job_id: str
    job_kind: str
    subject_key: str
    source_version: str
    payload: Mapping[str, Any]
    attempt: int
    failures: int
    max_attempts: int
    lease_token: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class JobRow:
    job_id: str
    job_kind: str
    subject_key: str
    source_version: str
    status: str
    claims: int
    failures: int
    max_attempts: int
    last_error: Optional[str]
    error_category: Optional[str]
    disposition_reason: Optional[str]
    next_run_at: Optional[datetime]
    enqueued_at: datetime
    finished_at: Optional[datetime]


_JOB_ROW_COLUMNS = (
    "job_id, job_kind, subject_key, source_version, status, claims, failures, "
    "max_attempts, last_error, error_category, disposition_reason, next_run_at, "
    "enqueued_at, finished_at"
)


@contextmanager
def transaction(con) -> Iterator[Any]:
    """One explicit transaction on an autocommit control-store connection."""
    con.execute("BEGIN")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _clip(message: Optional[str], limit: int = 2000) -> Optional[str]:
    if message is None:
        return None
    return message if len(message) <= limit else message[: limit - 1] + "…"


def _placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


class JobLedger:
    """Operations on ``pipeline_jobs`` for one registered control-store path."""

    def __init__(
        self,
        store_path: str | Path,
        *,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        require_memory_store(store_path)
        self.store_path = Path(store_path)
        self._jitter = jitter

    @contextmanager
    def connection(self, timeout: float | None = None):
        with control_plane_connection(self.store_path, timeout=timeout) as con:
            yield con

    # -- enqueue ------------------------------------------------------------ #

    def enqueue(
        self,
        job_kind: str,
        subject_key: str,
        *,
        source_version: str = "",
        payload: Optional[Mapping[str, Any]] = None,
        priority: int = 0,
        delay_seconds: float = 0.0,
        force: bool = False,
        con=None,
    ) -> EnqueueOutcome:
        """Open (or refresh) the live job for one subject and source version.

        With ``con`` the enqueue joins the caller's open transaction -- the
        summarizer enqueues embed/brief work in the same commit as the summary
        it depends on. Without it, the enqueue is its own transaction.

        ``force`` skips the "this version already succeeded/failed" and streak
        checks; operator requeue uses it.
        """
        if job_kind not in POLICIES:
            raise ValueError(f"unknown job kind {job_kind!r}")
        if con is not None:
            outcome = self._enqueue_in(
                con,
                job_kind,
                subject_key,
                source_version or "",
                payload,
                priority,
                delay_seconds,
                force,
            )
            return "already_queued" if outcome == "_raced" else outcome
        with self.connection() as own:
            for _ in range(3):
                with transaction(own):
                    outcome = self._enqueue_in(
                        own,
                        job_kind,
                        subject_key,
                        source_version or "",
                        payload,
                        priority,
                        delay_seconds,
                        force,
                    )
                if outcome != "_raced":
                    return outcome
        return "already_queued"

    @staticmethod
    def lock_subject(con, job_kind: str, subject_key: str) -> None:
        con.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            [json.dumps(["memory-job", job_kind, subject_key])],
        )

    def _enqueue_in(
        self,
        con,
        job_kind: str,
        subject_key: str,
        source_version: str,
        payload: Optional[Mapping[str, Any]],
        priority: int,
        delay_seconds: float,
        force: bool,
    ):
        # A missing live row cannot be row-locked. Serialize enqueues even
        # on the caller's transaction so a competing generation is never lost.
        self.lock_subject(con, job_kind, subject_key)
        policy = POLICIES[job_kind]
        payload_json = json.dumps(dict(payload or {}), sort_keys=True)
        live = con.execute(
            f"""SELECT job_id, status, source_version FROM pipeline_jobs
                 WHERE job_kind = ? AND subject_key = ?
                   AND status IN ({_placeholders(LIVE_STATUSES)})
                 FOR UPDATE""",
            [job_kind, subject_key, *LIVE_STATUSES],
        ).fetchone()
        if live is not None:
            job_id, status, live_version = live
            if live_version == source_version and not force:
                return "already_queued"
            if status in (PENDING, RETRY_WAIT):
                # Nothing has started on it: retarget in place with a fresh
                # budget. A different source is a different input.
                con.execute(
                    """UPDATE pipeline_jobs
                          SET source_version = ?, payload_json = ?, status = 'pending',
                              failures = 0, priority = GREATEST(priority, ?),
                              next_run_at = now() + make_interval(secs => ?),
                              last_error = NULL, error_category = NULL,
                              enqueued_at = now(), updated_at = now()
                        WHERE job_id = ?""",
                    [
                        source_version,
                        payload_json,
                        priority,
                        float(delay_seconds),
                        job_id,
                    ],
                )
                return "requeued"
            self._supersede_in(
                con,
                job_id,
                f"superseded by source version {source_version or '(none)'}",
            )
            replaced = True
        else:
            replaced = False
            if not force:
                prior = con.execute(
                    f"""SELECT status FROM pipeline_jobs
                         WHERE job_kind = ? AND subject_key = ? AND source_version = ?
                           AND status IN (?, ?, ?)
                         ORDER BY enqueued_at DESC LIMIT 1""",
                    [
                        job_kind,
                        subject_key,
                        source_version,
                        SUCCEEDED,
                        *FAILED_STATUSES,
                    ],
                ).fetchone()
                if prior is not None:
                    return "already_done" if prior[0] == SUCCEEDED else "already_failed"
                if policy.max_failed_streak is not None:
                    streak = self._failed_streak(con, job_kind, subject_key)
                    if streak >= policy.max_failed_streak:
                        return "suppressed"
        inserted = con.execute(
            f"""INSERT INTO pipeline_jobs
                  (job_id, job_kind, subject_key, source_version, payload_json,
                   status, priority, max_attempts, next_run_at)
                VALUES (gen_random_uuid()::text, ?, ?, ?, ?, 'pending', ?, ?,
                        now() + make_interval(secs => ?))
                ON CONFLICT (job_kind, subject_key)
                   WHERE status IN ({", ".join(repr(s) for s in LIVE_STATUSES)})
                DO NOTHING
                RETURNING job_id""",
            [
                job_kind,
                subject_key,
                source_version,
                payload_json,
                priority,
                policy.max_attempts,
                float(delay_seconds),
            ],
        ).fetchone()
        if inserted is None:
            # A concurrent enqueue opened the live row between our lookup and
            # insert. Re-run against it rather than dropping this version.
            return "_raced"
        return "requeued" if replaced else "queued"

    @staticmethod
    def _failed_streak(con, job_kind: str, subject_key: str) -> int:
        row = con.execute(
            """SELECT count(*) FROM pipeline_jobs
                WHERE job_kind = ? AND subject_key = ?
                  AND status IN ('dead_lettered', 'quarantined')
                  AND enqueued_at > COALESCE((
                    SELECT max(enqueued_at) FROM pipeline_jobs
                     WHERE job_kind = ? AND subject_key = ? AND status = 'succeeded'
                  ), '-infinity'::timestamptz)""",
            [job_kind, subject_key, job_kind, subject_key],
        ).fetchone()
        return int(row[0] or 0)

    # -- claim -------------------------------------------------------------- #

    def claim(
        self,
        job_kind: str,
        *,
        worker_id: str,
        limit: int = 1,
        lease_seconds: Optional[int] = None,
    ) -> list[ClaimedJob]:
        """Lease up to ``limit`` due jobs of one kind, oldest first."""
        policy = POLICIES[job_kind]
        lease = int(lease_seconds or policy.lease_seconds)
        self.reclaim_expired(job_kind)
        with self.connection() as con:
            with transaction(con):
                rows = con.execute(
                    """WITH due AS (
                         SELECT job_id FROM pipeline_jobs
                          WHERE job_kind = ? AND status IN ('pending', 'retry_wait')
                            AND next_run_at <= now()
                          ORDER BY priority DESC, next_run_at, enqueued_at
                          LIMIT ?
                          FOR UPDATE SKIP LOCKED
                       ), claimed AS (
                         UPDATE pipeline_jobs j
                            SET status = 'running', claims = j.claims + 1,
                                lease_owner = ?, lease_token = gen_random_uuid()::text,
                                lease_expires_at = now() + make_interval(secs => ?),
                                started_at = now(), updated_at = now()
                           FROM due WHERE j.job_id = due.job_id
                         RETURNING j.job_id, j.job_kind, j.subject_key, j.source_version,
                                   j.payload_json, j.claims, j.failures, j.max_attempts,
                                   j.lease_token, j.lease_expires_at, j.lease_owner,
                                   j.enqueued_at, j.priority, j.next_run_at
                       ), opened AS (
                         INSERT INTO pipeline_job_attempts
                           (job_id, attempt_no, worker_id, lease_token)
                         SELECT job_id, claims, lease_owner, lease_token FROM claimed
                       )
                       SELECT job_id, job_kind, subject_key, source_version, payload_json,
                              claims, failures, max_attempts, lease_token, lease_expires_at
                         FROM claimed
                        ORDER BY priority DESC, next_run_at, enqueued_at""",
                    [job_kind, max(1, int(limit)), worker_id, lease],
                ).fetchall()
        return [
            ClaimedJob(
                job_id=r[0],
                job_kind=r[1],
                subject_key=r[2],
                source_version=r[3],
                payload=json.loads(r[4] or "{}"),
                attempt=int(r[5]),
                failures=int(r[6]),
                max_attempts=int(r[7]),
                lease_token=r[8],
                lease_expires_at=r[9],
            )
            for r in rows
        ]

    def has_due(self, job_kind: str) -> bool:
        """Cheap idle check, so workers never warm a model on an empty queue."""
        with self.connection() as con:
            row = con.execute(
                """SELECT 1 FROM pipeline_jobs
                    WHERE job_kind = ?
                      AND ((status IN ('pending', 'retry_wait') AND next_run_at <= now())
                        OR (status = 'running' AND lease_expires_at < now()))
                    LIMIT 1""",
                [job_kind],
            ).fetchone()
        return row is not None

    def heartbeat(
        self, job: ClaimedJob, *, lease_seconds: Optional[int] = None
    ) -> bool:
        lease = int(lease_seconds or POLICIES[job.job_kind].lease_seconds)
        with self.connection() as con:
            row = con.execute(
                """UPDATE pipeline_jobs
                      SET lease_expires_at = now() + make_interval(secs => ?),
                          updated_at = now()
                    WHERE job_id = ? AND lease_token = ? AND status = 'running'
                      AND lease_expires_at > now()
                RETURNING job_id""",
                [lease, job.job_id, job.lease_token],
            ).fetchone()
        return row is not None

    # -- finish ------------------------------------------------------------- #

    def complete(
        self, job: ClaimedJob, *, con=None, metrics: Optional[Mapping[str, Any]] = None
    ) -> bool:
        """Mark a leased job succeeded. False if the lease is no longer ours.

        With ``con``, joins the caller's transaction so the derived row and the
        job transition commit together (and roll back together when this
        returns False and the caller aborts).
        """
        if con is not None:
            return self._complete_in(con, job, metrics)
        with self.connection() as own:
            with transaction(own):
                return self._complete_in(own, job, metrics)

    @staticmethod
    def _complete_in(con, job: ClaimedJob, metrics) -> bool:
        row = con.execute(
            """UPDATE pipeline_jobs
                  SET status = 'succeeded', finished_at = now(), updated_at = now(),
                      lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                      last_error = NULL, error_category = NULL
                WHERE job_id = ? AND lease_token = ? AND status = 'running'
                      AND lease_expires_at > now()
            RETURNING job_id""",
            [job.job_id, job.lease_token],
        ).fetchone()
        if row is None:
            return False
        con.execute(
            """UPDATE pipeline_job_attempts
                  SET finished_at = now(), result = 'succeeded', metrics_json = ?
                WHERE job_id = ? AND lease_token = ? AND finished_at IS NULL""",
            [
                json.dumps(dict(metrics)) if metrics else None,
                job.job_id,
                job.lease_token,
            ],
        )
        return True

    def fail(
        self,
        job: ClaimedJob,
        error: str,
        *,
        retryable: bool = True,
        category: Optional[str] = None,
    ) -> FailOutcome:
        """Spend one failure: retry with backoff, dead-letter, or quarantine."""
        with self.connection() as con:
            with transaction(con):
                row = con.execute(
                    """SELECT failures, max_attempts FROM pipeline_jobs
                        WHERE job_id = ? AND lease_token = ? AND status = 'running'
                      AND lease_expires_at > now()
                        FOR UPDATE""",
                    [job.job_id, job.lease_token],
                ).fetchone()
                if row is None:
                    return "stale"
                return self._spend_failure(
                    con,
                    job.job_id,
                    job.lease_token,
                    job.job_kind,
                    int(row[0]),
                    int(row[1]),
                    error,
                    retryable,
                    category,
                    attempt_result=(
                        "retryable_failed" if retryable else "terminal_failed"
                    ),
                )

    def _spend_failure(
        self,
        con,
        job_id: str,
        lease_token: str,
        job_kind: str,
        failures: int,
        max_attempts: int,
        error: str,
        retryable: bool,
        category: Optional[str],
        *,
        attempt_result: str,
    ) -> FailOutcome:
        policy = POLICIES[job_kind]
        spent = failures + 1
        error = _clip(error) or "unknown error"
        if not retryable:
            status: FailOutcome = "quarantined"
            reason = f"quarantined ({category or 'non_retryable'}): {error}"
        elif spent >= max_attempts:
            status = "dead_lettered"
            reason = f"exhausted {spent}/{max_attempts} attempts ({category or 'error'}): {error}"
        else:
            status = "retry_wait"
            reason = None
        delay = min(
            policy.retry_base_seconds * (2 ** max(spent - 1, 0)),
            policy.retry_max_seconds,
        ) * (1 + self._jitter(0, 0.2))
        con.execute(
            """UPDATE pipeline_jobs
                  SET status = ?, failures = ?, last_error = ?, error_category = ?,
                      disposition_reason = ?,
                      next_run_at = CASE WHEN ? = 'retry_wait'
                                         THEN now() + make_interval(secs => ?)
                                         ELSE next_run_at END,
                      finished_at = CASE WHEN ? = 'retry_wait' THEN NULL ELSE now() END,
                      lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                      updated_at = now()
                WHERE job_id = ?""",
            [
                status,
                spent,
                error,
                category,
                _clip(reason),
                status,
                float(delay),
                status,
                job_id,
            ],
        )
        con.execute(
            """UPDATE pipeline_job_attempts
                  SET finished_at = now(), result = ?, error_category = ?, error_message = ?
                WHERE job_id = ? AND lease_token = ? AND finished_at IS NULL""",
            [attempt_result, category, error, job_id, lease_token],
        )
        return status

    def release(self, job: ClaimedJob, *, delay_seconds: float, reason: str) -> bool:
        """Give a lease back without spending an attempt.

        For conditions outside the job (no backend configured, model host
        asleep): the job is fine, the worker just cannot run it right now.
        """
        with self.connection() as con:
            with transaction(con):
                row = con.execute(
                    """UPDATE pipeline_jobs
                          SET status = 'retry_wait', last_error = ?, error_category = 'released',
                              next_run_at = now() + make_interval(secs => ?),
                              lease_token = NULL, lease_owner = NULL,
                              lease_expires_at = NULL, updated_at = now()
                        WHERE job_id = ? AND lease_token = ? AND status = 'running'
                      AND lease_expires_at > now()
                    RETURNING job_id""",
                    [_clip(reason), float(delay_seconds), job.job_id, job.lease_token],
                ).fetchone()
                if row is None:
                    return False
                con.execute(
                    """UPDATE pipeline_job_attempts
                          SET finished_at = now(), result = 'released', error_message = ?
                        WHERE job_id = ? AND lease_token = ? AND finished_at IS NULL""",
                    [_clip(reason), job.job_id, job.lease_token],
                )
        return True

    def supersede(self, job: ClaimedJob, reason: str, *, con=None) -> bool:
        """Retire a leased job whose input moved on (stale generation)."""
        if con is not None:
            return self._supersede_leased(con, job, reason)
        with self.connection() as own:
            with transaction(own):
                return self._supersede_leased(own, job, reason)

    def _supersede_leased(self, con, job: ClaimedJob, reason: str) -> bool:
        row = con.execute(
            """SELECT 1 FROM pipeline_jobs
                WHERE job_id = ? AND lease_token = ? AND status = 'running'
                      AND lease_expires_at > now()
                FOR UPDATE""",
            [job.job_id, job.lease_token],
        ).fetchone()
        if row is None:
            return False
        self._supersede_in(con, job.job_id, reason)
        return True

    @staticmethod
    def _supersede_in(con, job_id: str, reason: str) -> None:
        con.execute(
            """UPDATE pipeline_job_attempts
                  SET finished_at = now(), result = 'superseded', error_message = ?
                WHERE job_id = ? AND finished_at IS NULL""",
            [_clip(reason), job_id],
        )
        con.execute(
            """UPDATE pipeline_jobs
                  SET status = 'superseded', disposition_reason = ?, finished_at = now(),
                      lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                      updated_at = now()
                WHERE job_id = ?""",
            [_clip(reason), job_id],
        )

    # -- recovery ----------------------------------------------------------- #

    def reclaim_expired(self, job_kind: Optional[str] = None) -> int:
        """Return expired leases to the due set, spending one failure each."""
        kinds = [job_kind] if job_kind else list(JOB_KINDS)
        reclaimed = 0
        with self.connection() as con:
            with transaction(con):
                rows = con.execute(
                    f"""SELECT job_id, lease_token, job_kind, failures, max_attempts
                          FROM pipeline_jobs
                         WHERE status = 'running' AND lease_expires_at < now()
                           AND job_kind IN ({_placeholders(kinds)})
                         FOR UPDATE SKIP LOCKED""",
                    kinds,
                ).fetchall()
                for job_id, token, kind, failures, max_attempts in rows:
                    self._spend_failure(
                        con,
                        job_id,
                        token,
                        kind,
                        int(failures),
                        int(max_attempts),
                        "lease expired before the worker finished",
                        True,
                        "lease_expired",
                        attempt_result="lease_expired",
                    )
                    reclaimed += 1
        return reclaimed

    # -- reads -------------------------------------------------------------- #

    def latest(self, job_kind: str, subject_key: str) -> Optional[JobRow]:
        with self.connection() as con:
            row = con.execute(
                f"""SELECT {_JOB_ROW_COLUMNS} FROM pipeline_jobs
                     WHERE job_kind = ? AND subject_key = ?
                     ORDER BY enqueued_at DESC, updated_at DESC LIMIT 1""",
                [job_kind, subject_key],
            ).fetchone()
        return JobRow(*row) if row is not None else None

    def jobs(
        self,
        job_kind: str,
        *,
        statuses: Sequence[str] = FAILED_STATUSES,
        limit: int = 100,
    ) -> list[JobRow]:
        with self.connection() as con:
            rows = con.execute(
                f"""SELECT {_JOB_ROW_COLUMNS} FROM pipeline_jobs
                     WHERE job_kind = ? AND status IN ({_placeholders(statuses)})
                     ORDER BY updated_at DESC LIMIT ?""",
                [job_kind, *statuses, max(1, int(limit))],
            ).fetchall()
        return [JobRow(*row) for row in rows]

    def stats(
        self, *, con=None, timeout: float | None = None
    ) -> dict[str, dict[str, Any]]:
        """Per-kind queue health for ``/readyz`` and ``drover_data_quality``."""
        if con is None:
            with self.connection(timeout=timeout) as own:
                return self.stats(con=own)
        return ledger_stats(con)


def ledger_stats(con) -> dict[str, dict[str, Any]]:
    """Per-kind health from one control-store connection (index-backed reads)."""
    out: dict[str, dict[str, Any]] = {
        kind: {
            "pending": 0,
            "retry_wait": 0,
            "running": 0,
            "expired_leases": 0,
            "oldest_lease_age_seconds": None,
            "oldest_pending_age_seconds": None,
            "dead_lettered": 0,
            "quarantined": 0,
            "last_success_at": None,
            "last_error": None,
        }
        for kind in JOB_KINDS
    }
    # job_kind is CHECK-constrained to JOB_KINDS, so every row has a slot.
    for row in con.execute("""SELECT job_kind,
                  count(*) FILTER (WHERE status = 'pending'),
                  count(*) FILTER (WHERE status = 'retry_wait'),
                  count(*) FILTER (WHERE status = 'running'),
                  count(*) FILTER (WHERE status = 'running' AND lease_expires_at < now()),
                  EXTRACT(EPOCH FROM now() - min(started_at) FILTER (WHERE status = 'running')),
                  EXTRACT(EPOCH FROM now() - min(enqueued_at)
                          FILTER (WHERE status IN ('pending', 'retry_wait')))
             FROM pipeline_jobs
            WHERE status IN ('pending', 'running', 'retry_wait')
            GROUP BY job_kind""").fetchall():
        out[row[0]].update(
            pending=int(row[1]),
            retry_wait=int(row[2]),
            running=int(row[3]),
            expired_leases=int(row[4]),
            oldest_lease_age_seconds=_seconds(row[5]),
            oldest_pending_age_seconds=_seconds(row[6]),
        )
    for job_kind, status, count in con.execute(
        """SELECT job_kind, status, count(*) FROM pipeline_jobs
            WHERE status IN ('dead_lettered', 'quarantined')
            GROUP BY job_kind, status"""
    ).fetchall():
        out[job_kind][status] = int(count)
    for job_kind in JOB_KINDS:
        success = con.execute(
            """SELECT finished_at FROM pipeline_jobs
                WHERE job_kind = ? AND status = 'succeeded'
                ORDER BY finished_at DESC LIMIT 1""",
            [job_kind],
        ).fetchone()
        if success is not None and success[0] is not None:
            out[job_kind]["last_success_at"] = success[0].isoformat()
        error = con.execute(
            """SELECT last_error FROM pipeline_jobs
                WHERE job_kind = ? AND last_error IS NOT NULL
                  AND status IN ('retry_wait', 'dead_lettered', 'quarantined')
                ORDER BY updated_at DESC LIMIT 1""",
            [job_kind],
        ).fetchone()
        if error is not None:
            out[job_kind]["last_error"] = _clip(error[0], 300)
    return out


def _seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    return round(max(0.0, float(value)), 1)
