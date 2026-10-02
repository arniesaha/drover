"""BriefWorker — drains ``brief_project`` ledger jobs into project_briefs (#480).

For each project (`<owner>/<name>`), pulls the most recent final session
summaries from ``session_memory`` (the summarizer stamps ``project_key`` on
them), asks the backend to synthesize a project-level brief, and upserts the
result into ``project_briefs`` in the PostgreSQL control store -- in the same
transaction that completes the job. The job ledger is the only queue; the
DuckDB ``brief_jobs`` table, the ledger shadow and the Redis brief stream are
gone from this path. Session counts and last activity are still read from the
analytical DuckDB (``tasks`` / canonical agent events), read-only.

Briefs are second-order summaries (summary-of-summaries). They benefit
from a stronger backend, so the worker selects with ``job_kind="project_brief"``
which prefers the API over local Ollama.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb

from drover.event_identity import canonical_agent_events_cte
from drover.server.briefs.prompt import build_brief_prompt
from drover.server.db import open_duckdb_connection
from drover.server.ledger import (
    BRIEF_PROJECT,
    LIVE_STATUSES,
    ClaimedJob,
    JobLedger,
    memory_store_available,
    transaction,
)
from drover.server.memory_store import MemoryRepository, ProjectBrief, SessionSummary
from drover.server.summarizer.backends import (
    BackendError,
    BackendReadinessError,
    LLMBackend,
    SummarizerBackendConfig,
    select_backend,
)

log = logging.getLogger("drover.briefs.worker")


_DEFAULT_RECENT_N = 8  # number of recent summaries fed to the brief prompt

# Release delays (seconds) for conditions outside the job; neither spends an
# attempt.
NO_BACKEND_RELEASE_S = 300.0
NOT_READY_RELEASE_S = 120.0


class _BriefInputError(Exception):
    """The job's input cannot produce a brief (non-retryable: quarantine)."""

    def __init__(self, message: str, category: str) -> None:
        super().__init__(message)
        self.category = category


class _LeaseLost(Exception):
    """``complete`` refused: the lease was reclaimed or superseded. Roll back."""


class BriefWorker:
    def __init__(
        self,
        *,
        duckdb_path: Path,
        backend: Optional[LLMBackend] = None,
        backend_config: Optional[SummarizerBackendConfig] = None,
        recent_n: int = _DEFAULT_RECENT_N,
        poll_interval_s: float = 30.0,
        worker_id: str = "briefs",
    ) -> None:
        # Both the control-store registration path (ledger, project_briefs)
        # and the analytical DuckDB (tasks, agent events).
        self.duckdb_path = Path(duckdb_path)
        self._backend = backend
        self._backend_config = backend_config
        self.recent_n = recent_n
        self.poll_interval_s = poll_interval_s
        self.worker_id = worker_id
        self._unavailable_logged = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="drover-briefs", daemon=True
        )
        self._thread.start()
        log.info("brief worker started")

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception:  # noqa: BLE001
                log.exception("brief drain loop crashed (will retry)")
            self._stop.wait(self.poll_interval_s)

    def _resolve_backend(self) -> Optional[LLMBackend]:
        if self._backend is not None:
            return self._backend
        if self._backend_config is None:
            return None
        try:
            return select_backend(job_kind="project_brief", config=self._backend_config)
        except BackendError as e:
            log.warning("brief backend selection failed: %s", e)
            return None

    def _ledger(self) -> Optional[JobLedger]:
        if not memory_store_available(self.duckdb_path):
            if not self._unavailable_logged:
                log.warning(
                    "brief worker: derived memory requires the PostgreSQL control "
                    "store; project briefs are disabled"
                )
                self._unavailable_logged = True
            return None
        return JobLedger(self.duckdb_path)

    def drain_once(self) -> int:
        """Process one due brief job. Returns 1 if one was handled, 0 otherwise.

        "Handled" includes releasing a job because the backend is missing or
        not ready; an empty queue never resolves or warms the backend.
        """
        ledger = self._ledger()
        if ledger is None or not ledger.has_due(BRIEF_PROJECT):
            return 0
        jobs = ledger.claim(BRIEF_PROJECT, worker_id=self.worker_id, limit=1)
        if not jobs:
            return 0
        job = jobs[0]
        project_key = job.subject_key

        backend = self._resolve_backend()
        if backend is None:
            log.warning("brief %s: no backend configured; releasing", project_key)
            ledger.release(
                job,
                delay_seconds=NO_BACKEND_RELEASE_S,
                reason="no backend configured for project briefs",
            )
            return 1
        not_ready = self._backend_not_ready(backend)
        if not_ready is not None:
            log.warning("brief %s not ready; releasing: %s", project_key, not_ready)
            ledger.release(job, delay_seconds=NOT_READY_RELEASE_S, reason=not_ready)
            return 1

        try:
            self._regenerate_brief(ledger, job, backend)
        except _BriefInputError as exc:
            log.warning("brief %s quarantined: %s", project_key, exc)
            ledger.fail(job, str(exc), retryable=False, category=exc.category)
        except _LeaseLost:
            log.info("brief %s lost its lease before commit; discarded", project_key)
        except BackendError as exc:
            log.warning("brief %s failed: %s", project_key, exc)
            ledger.fail(job, str(exc), retryable=True, category="brief_backend")
        except Exception as exc:  # noqa: BLE001
            log.warning("brief %s failed: %s", project_key, exc)
            ledger.fail(job, str(exc), retryable=True, category="brief_error")
        return 1

    @staticmethod
    def _backend_not_ready(backend: LLMBackend) -> Optional[str]:
        """None when ready; otherwise the retryable readiness failure message."""
        if not hasattr(backend, "ensure_ready"):
            return None
        try:
            backend.ensure_ready()  # type: ignore[attr-defined]
            return None
        except BackendReadinessError as e:
            return f"retryable local model readiness failure: {e}"
        except BackendError as e:
            return f"retryable backend readiness failure: {e}"

    def _project_stats(
        self, owner: str, name: str, summaries: list[SessionSummary]
    ) -> tuple[int, Optional[datetime]]:
        """Session count and last activity from the analytical DuckDB.

        ``tasks`` first, canonical agent events when no task row carries the
        repo. Read-only and best-effort: if the analytical store cannot be
        read, fall back to what the summaries themselves say rather than fail
        a brief over a statistic.
        """
        try:
            con = open_duckdb_connection(
                self.duckdb_path, read_only=True, role="diagnostic"
            )
            try:
                session_count, last_activity = con.execute(
                    """SELECT sum(COALESCE(session_count, 0)), max(last_activity_at)
                       FROM tasks
                       WHERE repo_owner=? AND repo_name=?""",
                    [owner, name],
                ).fetchone()
                if session_count is None and last_activity is None:
                    session_count, last_activity = con.execute(
                        f"""WITH {canonical_agent_events_cte()}
                           SELECT count(DISTINCT session_id),
                                  max(TRY_CAST(timestamp AS TIMESTAMP))
                           FROM canonical_agent_events
                           WHERE repo_owner=? AND repo_name=?""",
                        [owner, name],
                    ).fetchone()
            finally:
                con.close()
        except (duckdb.Error, OSError) as e:
            log.warning("brief %s/%s: analytical stats unavailable: %s", owner, name, e)
            session_count, last_activity = None, None
        if not session_count:
            session_count = len(summaries)
        if last_activity is None:
            ended = [s.ended_at for s in summaries if s.ended_at is not None]
            last_activity = max(ended) if ended else None
        return int(session_count or 0), last_activity

    def _regenerate_brief(
        self, ledger: JobLedger, job: ClaimedJob, backend: LLMBackend
    ) -> None:
        project_key = job.subject_key
        owner, _, name = project_key.partition("/")
        if not owner or not name:
            raise _BriefInputError(
                f"malformed project_key: {project_key!r} (expected owner/name)",
                "malformed_project_key",
            )

        repo = MemoryRepository(self.duckdb_path)
        summaries = repo.recent_summaries(project_key=project_key, limit=self.recent_n)
        if not summaries:
            # A brief job is enqueued in the same commit as the summary that
            # triggered it, so no summaries means the input vanished.
            raise _BriefInputError(
                f"no session summaries for project {project_key}", "no_summaries"
            )
        session_count, last_activity = self._project_stats(owner, name, summaries)

        summary_dicts: list[dict[str, Any]] = []
        for s in summaries:
            row = s.as_dict()
            row["ended_at"] = (
                s.ended_at.isoformat()
                if isinstance(s.ended_at, datetime)
                else s.ended_at
            )
            summary_dicts.append(row)
        prompt = build_brief_prompt(
            summaries=summary_dicts,
            project_key=project_key,
            repo_owner=owner,
            repo_name=name,
            session_count=session_count,
            last_activity_at=last_activity.isoformat() if last_activity else None,
        )

        llm = backend.summarize(prompt)  # same JSON contract

        # Aggregate key_files from input summaries (top-N by frequency)
        key_files = _top_files([list(s.files_touched) for s in summaries], n=10)
        payload = job.payload or {}
        brief = ProjectBrief(
            project_key=project_key,
            repo_owner=owner,
            repo_name=name,
            brief_md=llm.get("brief_md") or "",
            recent_themes_md=llm.get("recent_themes_md") or "",
            key_files=tuple(llm.get("key_files") or key_files),
            open_questions=tuple(llm.get("open_questions") or ()),
            next_steps_md=llm.get("next_steps_md") or "",
            session_count=session_count,
            last_activity_at=last_activity,
            source_session_id=payload.get("source_session_id"),
            source_version=payload.get("source_version") or job.source_version or None,
            generator_model=getattr(backend, "model", None),
        )
        with ledger.connection() as con:
            with transaction(con):
                repo.put_brief(con, brief)
                if not ledger.complete(job, con=con):
                    raise _LeaseLost()


def enqueue_brief(store_path: Path, project_key: str) -> str:
    """Open a brief job for one project in the PG ledger.

    Briefs decay with activity, so a project whose last brief succeeded can
    always be re-run (``force``). A live job is left alone. Returns one of
    ``queued``, ``requeued`` (a previous brief job exists), ``already_queued``,
    or ``unavailable`` when the PostgreSQL control store is not configured.
    """
    if not memory_store_available(store_path):
        log.warning(
            "enqueue_brief %s: derived memory requires the PostgreSQL control store",
            project_key,
        )
        return "unavailable"
    ledger = JobLedger(store_path)
    latest = ledger.latest(BRIEF_PROJECT, project_key)
    if latest is not None and latest.status in LIVE_STATUSES:
        return "already_queued"
    outcome = ledger.enqueue(BRIEF_PROJECT, project_key, force=True)
    if outcome == "queued" and latest is not None:
        return "requeued"
    return outcome


def _active_project_keys(store_path: Path, *, hours: int) -> list[str]:
    """Projects with a final session summary that ended within ``hours``."""
    since = datetime.now(timezone.utc) - timedelta(hours=max(0, int(hours)))
    with MemoryRepository(store_path).connection() as con:
        rows = con.execute(
            """SELECT DISTINCT project_key FROM session_memory
                WHERE phase = 'final' AND project_key LIKE '%_/_%'
                  AND ended_at >= ?
                ORDER BY project_key""",
            [since],
        ).fetchall()
    return [row[0] for row in rows]


def enqueue_briefs_for_active_projects(
    store_path: Path, *, hours: int = 168
) -> list[tuple[str, str]]:
    """Enqueue a brief for every attributed project with recent activity.

    Active = at least one final session summary for the project (its
    ``session_memory.project_key``) ended within ``hours``. Default window is
    7 days. Returns ``(project_key, enqueue_outcome)`` pairs; empty (logged)
    when the PostgreSQL control store is not configured.
    """
    if not memory_store_available(store_path):
        log.warning(
            "enqueue_briefs_for_active_projects: derived memory requires the "
            "PostgreSQL control store"
        )
        return []
    return [
        (pk, enqueue_brief(store_path, pk))
        for pk in _active_project_keys(store_path, hours=hours)
    ]


def _top_files(file_lists: list[list[str]], *, n: int) -> list[str]:
    counts: dict[str, int] = {}
    for files in file_lists:
        for f in files or []:
            counts[f] = counts.get(f, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f for f, _ in ranked[:n]]
