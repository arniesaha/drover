"""BriefWorker — drains brief_jobs into project_briefs.

For each project (`<owner>/<name>`), pulls the most recent session
summaries, asks the backend to synthesize a project-level brief, and
upserts the result into ``project_briefs``.

Briefs are second-order summaries (summary-of-summaries). They benefit
from a stronger backend, so the worker selects with ``job_kind="project_brief"``
which prefers the API over local Ollama.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import duckdb

from drover.event_identity import canonical_agent_events_cte
from drover.server import ledger_shadow
from drover.server.briefs.prompt import build_brief_prompt
from drover.server.db import open_duckdb_connection
from drover.server.jobs import Delivery
from drover.server.summarizer.backends import (
    BackendError,
    BackendReadinessError,
    LLMBackend,
    SummarizerBackendConfig,
    select_backend,
)

log = logging.getLogger("drover.briefs.worker")


_DEFAULT_RECENT_N = 8  # number of recent summaries fed to the brief prompt


class BriefWorker:
    def __init__(
        self,
        *,
        duckdb_path: Path,
        backend: Optional[LLMBackend] = None,
        backend_config: Optional[SummarizerBackendConfig] = None,
        recent_n: int = _DEFAULT_RECENT_N,
        poll_interval_s: float = 30.0,
        job_stream: Optional[object] = None,
        worker_id: str = "briefs",
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self._backend = backend
        self._backend_config = backend_config
        self.recent_n = recent_n
        self.poll_interval_s = poll_interval_s
        self.job_stream = job_stream
        self.worker_id = worker_id
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # Crash recovery (AGE-45): reconcile crashed in-flight work from DuckDB.
        ledger_shadow.recover_runnable(
            self.duckdb_path, job_kind="regenerate_project_brief"
        )
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

    def drain_once(self) -> int:
        """Process one pending brief job. Returns 1 if one was handled, 0 otherwise."""
        if self.job_stream is None:
            pending_project = self._oldest_pending_project()
            if pending_project is None:
                return 0
            backend = self._resolve_backend()
            if not self._ensure_backend_ready(pending_project, backend, delivery=None):
                return 1
        else:
            backend = None

        claim = (
            self._claim_stream_job() if self.job_stream else self._claim_duckdb_job()
        )
        if claim is None:
            return 0
        project_key, delivery = claim
        if project_key is None:
            return 1

        if self.job_stream is not None:
            backend = self._resolve_backend()
            if not self._ensure_backend_ready(project_key, backend, delivery=delivery):
                return 1

        # Shadow durable-ledger job lifecycle (AGE-44); best-effort.
        ledger_job_id = ledger_shadow.begin_attempt(
            self.duckdb_path,
            job_kind="regenerate_project_brief",
            subject_key=project_key,
            subject_kind="project",
            worker_id="briefs",
        )

        try:
            self._regenerate_brief(project_key, backend)
        except Exception as exc:  # noqa: BLE001
            log.warning("brief %s failed: %s", project_key, exc)
            self._mark_errored(project_key, str(exc))
            if delivery is not None:
                self.job_stream.fail(delivery.id, str(exc))
            ledger_shadow.retry(self.duckdb_path, ledger_job_id, error_message=str(exc))
            return 1
        self._mark_done(project_key)
        ledger_shadow.succeed(
            self.duckdb_path,
            ledger_job_id,
            artifact_kind="project_brief",
            subject_key=project_key,
            storage_uri=f"project_briefs/{project_key}",
        )
        if delivery is not None:
            self.job_stream.ack(delivery.id)
        return 1

    def _oldest_pending_project(self) -> Optional[str]:
        con = open_duckdb_connection(self.duckdb_path)
        try:
            row = con.execute("""SELECT b.project_key FROM brief_jobs b
                   WHERE b.status='pending'
                     AND (b.source_version IS NULL OR EXISTS (
                       SELECT 1 FROM summarize_jobs s
                        WHERE s.session_id=b.source_session_id
                          AND s.source_version IS NOT DISTINCT FROM b.source_version
                          AND s.status='done'
                     ))
                   ORDER BY enqueued_at ASC LIMIT 1""").fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def _ensure_backend_ready(
        self,
        project_key: str,
        backend: Optional[LLMBackend],
        *,
        delivery: Optional[Delivery],
    ) -> bool:
        if backend is None or not hasattr(backend, "ensure_ready"):
            return True
        try:
            backend.ensure_ready()  # type: ignore[attr-defined]
            return True
        except BackendReadinessError as e:
            message = f"retryable local model readiness failure: {e}"
        except BackendError as e:
            message = f"retryable backend readiness failure: {e}"
        log.warning("brief %s not ready; leaving retryable: %s", project_key, message)
        self._mark_retryable(project_key, message)
        if delivery is not None:
            self.job_stream.fail(delivery.id, message)
        return False

    def _claim_duckdb_job(self) -> Optional[tuple[str, Optional[Delivery]]]:
        con = open_duckdb_connection(self.duckdb_path)
        try:
            row = con.execute("""SELECT b.project_key, b.attempts FROM brief_jobs b
                   WHERE b.status='pending'
                     AND (b.source_version IS NULL OR EXISTS (
                       SELECT 1 FROM summarize_jobs s
                        WHERE s.session_id=b.source_session_id
                          AND s.source_version IS NOT DISTINCT FROM b.source_version
                          AND s.status='done'
                     ))
                   ORDER BY enqueued_at ASC LIMIT 1""").fetchone()
            if row is None:
                return None
            project_key, attempts = row[0], row[1] or 0
            claimed = con.execute(
                """UPDATE brief_jobs b
                   SET status='running', attempts=?, updated_at=now()
                   WHERE b.project_key=? AND b.status='pending'
                     AND (b.source_version IS NULL OR EXISTS (
                       SELECT 1 FROM summarize_jobs s
                        WHERE s.session_id=b.source_session_id
                          AND s.source_version IS NOT DISTINCT FROM b.source_version
                          AND s.status='done'
                     ))
                   RETURNING b.project_key""",
                [attempts + 1, project_key],
            ).fetchone()
            if claimed is None:
                return None
        finally:
            con.close()
        return project_key, None

    def _claim_stream_job(self) -> Optional[tuple[Optional[str], Optional[Delivery]]]:
        deliveries = self.job_stream.read_group(self.worker_id, count=1)
        if not deliveries:
            deliveries = self.job_stream.reclaim(self.worker_id, count=1)
        if not deliveries:
            return None
        delivery = deliveries[0]
        project_key = delivery.fields.get("project_key")
        if not project_key:
            self.job_stream.fail(delivery.id, "missing project_key")
            return None, delivery
        project_key = str(project_key)
        delivery_source_session_id = delivery.fields.get("source_session_id")
        delivery_source_version = delivery.fields.get("source_version")
        if delivery_source_session_id is not None:
            delivery_source_session_id = str(delivery_source_session_id)
        if delivery_source_version is not None:
            delivery_source_version = str(delivery_source_version)

        con = open_duckdb_connection(self.duckdb_path)
        try:
            row = con.execute(
                """SELECT status, attempts, source_session_id, source_version
                     FROM brief_jobs WHERE project_key=?""",
                [project_key],
            ).fetchone()
            if delivery_source_version is not None:
                if row is None:
                    self.job_stream.ack(delivery.id)
                    return None, None
                claimed = con.execute(
                    """UPDATE brief_jobs b
                          SET status='running',
                              attempts=COALESCE(attempts, 0)+1,
                              updated_at=now()
                        WHERE b.project_key=?
                          AND b.status NOT IN ('done', 'superseded')
                          AND b.source_session_id IS NOT DISTINCT FROM ?
                          AND b.source_version IS NOT DISTINCT FROM ?
                          AND EXISTS (
                            SELECT 1 FROM summarize_jobs s
                             WHERE s.session_id=b.source_session_id
                               AND s.source_version IS NOT DISTINCT FROM b.source_version
                               AND s.status='done'
                          )
                        RETURNING b.project_key""",
                    [project_key, delivery_source_session_id, delivery_source_version],
                ).fetchone()
                if claimed is None:
                    self.job_stream.ack(delivery.id)
                    return None, None
                return project_key, delivery
            if row is None:
                con.execute(
                    """INSERT INTO brief_jobs
                       (project_key, status, attempts, updated_at)
                       VALUES (?, 'running', 1, now())""",
                    [project_key],
                )
                return project_key, delivery
            status, attempts = row[0], row[1] or 0
            if status == "done":
                self.job_stream.ack(delivery.id)
                return None, None
            con.execute(
                """UPDATE brief_jobs
                   SET status='running', attempts=?, updated_at=now()
                   WHERE project_key=?""",
                [attempts + 1, project_key],
            )
            return project_key, delivery
        finally:
            con.close()

    def _regenerate_brief(
        self, project_key: str, backend: Optional[LLMBackend]
    ) -> None:
        if backend is None:
            raise RuntimeError("no backend configured for project briefs")

        owner, _, name = project_key.partition("/")
        if not owner or not name:
            raise RuntimeError(
                f"malformed project_key: {project_key!r} (expected owner/name)"
            )

        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            cur = con.execute(
                """SELECT ss.session_id, ss.agent_id, ss.ended_at,
                          ss.summary_md, ss.next_steps_md, ss.files_touched,
                          ss.open_questions
                   FROM session_summaries ss
                   JOIN tasks t USING (task_id)
                   WHERE t.repo_owner = ? AND t.repo_name = ?
                   ORDER BY ss.ended_at DESC
                   LIMIT ?""",
                [owner, name, self.recent_n],
            )
            cols = [d[0] for d in cur.description]
            summaries = [dict(zip(cols, r)) for r in cur.fetchall()]
            # Fallback: if no task linkage, use raw agent_events for recency stats
            if not summaries:
                fallback = con.execute(
                    f"""WITH {canonical_agent_events_cte()}
                       SELECT ss.session_id, ss.agent_id, ss.ended_at,
                              ss.summary_md, ss.next_steps_md, ss.files_touched,
                              ss.open_questions
                       FROM session_summaries ss
                       WHERE ss.session_id IN (
                         SELECT DISTINCT session_id FROM canonical_agent_events
                          WHERE repo_owner=? AND repo_name=?
                       )
                       ORDER BY ss.ended_at DESC
                       LIMIT ?""",
                    [owner, name, self.recent_n],
                )
                cols = [d[0] for d in fallback.description]
                summaries = [dict(zip(cols, r)) for r in fallback.fetchall()]

            session_count = None
            last_activity = None
            if summaries:
                task_stats = con.execute(
                    """SELECT sum(COALESCE(session_count, 0)), max(last_activity_at)
                       FROM tasks
                       WHERE repo_owner=? AND repo_name=?""",
                    [owner, name],
                ).fetchone()
                if task_stats:
                    session_count, last_activity = task_stats
            else:
                session_count = con.execute(
                    f"""WITH {canonical_agent_events_cte()}
                       SELECT count(DISTINCT session_id) FROM canonical_agent_events
                       WHERE repo_owner=? AND repo_name=?""",
                    [owner, name],
                ).fetchone()[0]
                last_activity = con.execute(
                    f"""WITH {canonical_agent_events_cte()}
                       SELECT max(TRY_CAST(timestamp AS TIMESTAMP)) FROM canonical_agent_events
                       WHERE repo_owner=? AND repo_name=?""",
                    [owner, name],
                ).fetchone()[0]
        finally:
            con.close()

        if not summaries:
            raise RuntimeError(f"no session_summaries for project {project_key}")

        prompt = build_brief_prompt(
            summaries=[
                {
                    **s,
                    "ended_at": (
                        s["ended_at"].isoformat()
                        if isinstance(s.get("ended_at"), datetime)
                        else s.get("ended_at")
                    ),
                }
                for s in summaries
            ],
            project_key=project_key,
            repo_owner=owner,
            repo_name=name,
            session_count=session_count or 0,
            last_activity_at=last_activity.isoformat() if last_activity else None,
        )

        try:
            llm = backend.summarize(prompt)  # same JSON contract
        except BackendError as e:
            raise RuntimeError(str(e)) from e

        # Aggregate key_files from input summaries (top-N by frequency)
        key_files = _top_files([s.get("files_touched") or [] for s in summaries], n=10)

        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.execute(
                """INSERT OR REPLACE INTO project_briefs
                   (project_key, repo_owner, repo_name, brief_md, recent_themes_md,
                    key_files, open_questions, next_steps_md,
                    session_count, last_activity_at, generator_model, generated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())""",
                [
                    project_key,
                    owner,
                    name,
                    llm.get("brief_md") or "",
                    llm.get("recent_themes_md") or "",
                    list(llm.get("key_files") or key_files),
                    list(llm.get("open_questions") or []),
                    llm.get("next_steps_md") or "",
                    int(session_count or 0),
                    last_activity,
                    backend.model,
                ],
            )
        finally:
            con.close()

    def _mark_done(self, project_key: str) -> None:
        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.execute(
                "UPDATE brief_jobs SET status='done', last_error=NULL, updated_at=now() WHERE project_key=?",
                [project_key],
            )
        finally:
            con.close()

    def _mark_errored(self, project_key: str, message: str) -> None:
        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.execute(
                "UPDATE brief_jobs SET status='errored', last_error=?, updated_at=now() WHERE project_key=?",
                [message, project_key],
            )
        finally:
            con.close()

    def _mark_retryable(self, project_key: str, message: str) -> None:
        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.execute(
                "UPDATE brief_jobs SET status='pending', last_error=?, updated_at=now() WHERE project_key=?",
                [message, project_key],
            )
        finally:
            con.close()


def enqueue_brief(duckdb_path: Path, project_key: str) -> str:
    """Idempotent: insert a pending brief_jobs row, or re-queue an errored one.

    Returns one of: ``queued``, ``already_queued``, ``requeued``, ``already_done``.
    """
    con = open_duckdb_connection(duckdb_path)
    try:
        existing = con.execute(
            "SELECT status FROM brief_jobs WHERE project_key=?",
            [project_key],
        ).fetchone()
        if existing:
            status = existing[0]
            if status == "done":
                # done briefs can always be re-enqueued (briefs decay with activity)
                con.execute(
                    "UPDATE brief_jobs SET status='pending', last_error=NULL, updated_at=now() WHERE project_key=?",
                    [project_key],
                )
                return "requeued"
            if status in ("pending", "running"):
                return "already_queued"
            con.execute(
                "UPDATE brief_jobs SET status='pending', last_error=NULL, updated_at=now() WHERE project_key=?",
                [project_key],
            )
            return "requeued"
        con.execute(
            "INSERT INTO brief_jobs (project_key, status, attempts) VALUES (?, 'pending', 0)",
            [project_key],
        )
    finally:
        con.close()
    return "queued"


def enqueue_briefs_for_active_projects(
    duckdb_path: Path, *, hours: int = 168
) -> list[tuple[str, str]]:
    """Enqueue a brief for every attributed project with recent activity.

    Active = the project's task has ``last_activity_at`` within ``hours``
    AND has at least one linked session_summary. Default window is 7 days.

    Returns the list of ``(project_key, enqueue_outcome)`` for the caller.
    """
    con = open_duckdb_connection(duckdb_path, read_only=True, role="diagnostic")
    try:
        rows = con.execute(
            f"""SELECT DISTINCT t.repo_owner || '/' || t.repo_name AS project_key
                  FROM tasks t
                  JOIN session_summaries ss ON ss.task_id = t.task_id
                 WHERE t.repo_owner IS NOT NULL
                   AND t.repo_name  IS NOT NULL
                   AND t.last_activity_at >= now() - INTERVAL {int(hours)} HOUR"""
        ).fetchall()
    finally:
        con.close()
    return [(pk, enqueue_brief(duckdb_path, pk)) for (pk,) in rows]


def _top_files(file_lists: list[list[str]], *, n: int) -> list[str]:
    counts: dict[str, int] = {}
    for files in file_lists:
        for f in files or []:
            counts[f] = counts.get(f, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f for f, _ in ranked[:n]]
