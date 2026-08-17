"""SummarizerWorker — drains summarize_jobs into session_summaries.

Designed for both threaded poll-loop use (start/stop) and one-shot
drain (drain_once for tests / explicit catch-up runs).

Each pending job:
  1. Mark status='running' (atomic update).
  2. Read session events from agent_events.
  3. Compute deterministic fields (files_touched, tools_used).
  4. Build prompt → call backend → parse JSON.
  5. Fence persistence against the claimed source version.
  6. Atomically write session_summaries and mark status='done'.
On failure: increment the generation's failure count, then schedule retry_wait
or dead-letter exactly the fifth failure. A dead letter also spends one of the
session's ``SUMMARY_MAX_DEAD_LETTERS`` generations; success clears that streak.

Backend selection prefers the claude-code CLI unless Anthropic credentials are
configured under a cloud/hybrid policy. See ``backends.select_backend``.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import duckdb

from drover.event_identity import canonical_agent_events_cte
from drover.server import ledger_shadow
from drover.server.db import open_duckdb_connection
from drover.server.jobs import Delivery
from drover.server.ledger import ArtifactSpec, Ledger
from drover.server.summarizer.backends import (
    BackendError,
    LLMBackend,
    SummarizerBackendConfig,
    select_backend,
)
from drover.server.summarizer.client import (
    DEFAULT_MODEL,
    NoApiKeyError,
    SummarizerClientError,
    call_claude_summary,
)
from drover.server.summarizer.derive import compute_files_touched, compute_tools_used
from drover.server.summarizer.jobs import (
    finish_summary_failure,
    flush_summary_publications,
)
from drover.server.summarizer.prompt import build_summary_prompt

log = logging.getLogger("drover.summarizer.worker")


@dataclass(frozen=True)
class _SummaryCompletion:
    project_key: Optional[str]
    brief_outcome: Optional[str]
    embed_outcome: str


def _open_summarizer_db(duckdb_path: Path) -> duckdb.DuckDBPyConnection:
    return open_duckdb_connection(duckdb_path, role="summarizer")


def _session_agent_events_ctes() -> str:
    return f"""
session_agent_events AS (
  SELECT *
  FROM agent_events
  WHERE session_id = ?
),
{canonical_agent_events_cte(source="session_agent_events")}
""".strip()


class SummarizerWorker:
    def __init__(
        self,
        *,
        duckdb_path: Path,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        poll_interval_s: float = 5.0,
        _llm_call: Optional[Callable[..., dict]] = None,
        backend: Optional[LLMBackend] = None,
        backend_config: Optional[SummarizerBackendConfig] = None,
        job_kind: str = "incremental",
        batch_size: int = 1,
        job_stream: Optional[object] = None,
        brief_job_stream: Optional[object] = None,
        embed_job_stream: Optional[object] = None,
        worker_id: str = "summarizer",
        _clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        _jitter: Callable[[float, float], float] = random.uniform,
        _before_success_effects: Callable[[], None] = lambda: None,
        _before_failure_finish: Callable[[], None] = lambda: None,
        _after_completion_commit: Callable[[], None] = lambda: None,
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self.api_key = api_key
        self.model = model
        self.poll_interval_s = poll_interval_s
        self._llm_call = _llm_call or call_claude_summary
        self._backend = backend
        self._backend_config = backend_config
        self.job_kind = job_kind
        self.batch_size = max(1, int(batch_size))
        self.job_stream = job_stream
        self.brief_job_stream = brief_job_stream
        self.embed_job_stream = embed_job_stream
        self.worker_id = worker_id
        self._clock = _clock
        self._jitter = _jitter
        self._before_success_effects = _before_success_effects
        self._before_failure_finish = _before_failure_finish
        self._after_completion_commit = _after_completion_commit
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _resolve_backend(self) -> Optional[LLMBackend]:
        """Return an explicit backend, or pick one from config — or None.

        ``None`` means fall back to ``self._llm_call`` (legacy path used by
        existing tests). Errors during selection are logged and swallowed
        so the worker can still mark jobs errored cleanly.
        """
        if self._backend is not None:
            return self._backend
        if self._backend_config is None:
            return None
        try:
            return select_backend(job_kind=self.job_kind, config=self._backend_config)
        except BackendError as e:
            log.warning("backend selection failed: %s", e)
            return None

    def _db_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            return now
        return now.astimezone(timezone.utc).replace(tzinfo=None)

    # --- public lifecycle ---

    def start(self) -> None:
        if self._thread is not None:
            return
        # Crash recovery (AGE-45): reconcile crashed in-flight work from DuckDB
        # before draining, so a job stranded 'running'/'leased' by a previous
        # process becomes runnable again.
        ledger_shadow.recover_runnable(self.duckdb_path, job_kind="summarize_session")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="drover-summarizer", daemon=True
        )
        self._thread.start()
        log.info("summarizer worker started (model=%s)", self.model)

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
                log.exception("summarizer drain loop crashed (will retry)")
            self._stop.wait(self.poll_interval_s)

    # --- core ---

    def drain_once(self) -> int:
        """Process one pending job. Returns 1 if a job was handled, 0 otherwise."""
        return self.drain_batch(max_jobs=1)

    def drain_batch(self, *, max_jobs: Optional[int] = None) -> int:
        """Process up to ``max_jobs`` pending jobs back-to-back.

        Defaults to ``self.batch_size``. Returns the number of jobs
        processed (≥0). Intended for use with a local backend so the
        WoL cold-start amortizes across the batch.
        """
        if max_jobs is None:
            max_jobs = self.batch_size

        if self.job_stream is not None:
            self._flush_stream_outbox()
            return self._drain_stream_batch(max_jobs=max_jobs)

        # Check for pending jobs BEFORE selecting a backend so that backend
        # fallback warnings are never emitted on idle poll ticks (fixes #55).
        con = _open_summarizer_db(self.duckdb_path)
        try:
            con.execute(
                """UPDATE summarize_jobs
                      SET status='pending', next_run_at=NULL, updated_at=?
                    WHERE status='retry_wait' AND next_run_at <= ?""",
                [self._db_now(), self._db_now()],
            )
            row = con.execute(
                "SELECT 1 FROM summarize_jobs WHERE status='pending' LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return 0

        backend = self._resolve_backend()
        # Pre-warm: if it's an Ollama-style backend, do the wake check now
        # so all jobs in the batch land warm.
        if backend is not None and hasattr(backend, "ensure_ready"):
            try:
                backend.ensure_ready()
            except BackendError as e:
                log.warning("backend ensure_ready failed: %s", e)
                # Fall through; per-job summarize will raise & mark errored.

        processed = 0
        for _ in range(max_jobs):
            n = self._drain_one(backend)
            if n == 0:
                break
            processed += n
        return processed

    def _flush_stream_outbox(self) -> None:
        con = _open_summarizer_db(self.duckdb_path)
        try:
            flush_summary_publications(con, self.job_stream)
        except Exception:  # noqa: BLE001
            log.warning("summary publication outbox flush failed", exc_info=True)
        finally:
            con.close()

    def _drain_stream_batch(self, *, max_jobs: int) -> int:
        """Drain stream deliveries without warming the backend on idle ticks."""
        processed = 0
        backend: Optional[LLMBackend] = None
        backend_checked = False
        for _ in range(max_jobs):
            claim = self._claim_stream_job()
            if claim is None:
                break
            session_id, source_version, delivery = claim
            if session_id is None:
                processed += 1
                continue
            if not backend_checked:
                backend = self._resolve_backend()
                if backend is not None and hasattr(backend, "ensure_ready"):
                    try:
                        backend.ensure_ready()
                    except BackendError as e:
                        log.warning("backend ensure_ready failed: %s", e)
                backend_checked = True
            processed += self._process_claim(
                session_id=session_id,
                source_version=source_version,
                delivery=delivery,
                backend=backend,
            )
        return processed

    def _drain_one(self, backend: Optional[LLMBackend]) -> int:
        claim = (
            self._claim_stream_job() if self.job_stream else self._claim_duckdb_job()
        )
        if claim is None:
            return 0
        session_id, source_version, delivery = claim
        if session_id is None:
            return 1
        return self._process_claim(
            session_id=session_id,
            source_version=source_version,
            delivery=delivery,
            backend=backend,
        )

    def _process_claim(
        self,
        *,
        session_id: str,
        source_version: Optional[str],
        delivery: Optional[Delivery],
        backend: Optional[LLMBackend],
    ) -> int:
        # Shadow durable-ledger job lifecycle (AGE-44): open+lease the logical
        # summarize job alongside the live summarize_jobs row. Best-effort —
        # serving still reads summarize_jobs / session_summaries.
        ledger_job_id = ledger_shadow.begin_attempt(
            self.duckdb_path,
            job_kind="summarize_session",
            subject_key=session_id,
            subject_kind="session",
            worker_id="summarizer",
        )

        try:
            completion = self._summarize_session(
                session_id,
                source_version,
                backend,
                ledger_job_id=ledger_job_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("summarize %s failed: %s", session_id, exc)
            outcome = self._finish_failure(
                session_id,
                source_version,
                exc,
                ledger_job_id=ledger_job_id,
            )
            if delivery is not None:
                if outcome in ("dead_lettered", "stale"):
                    self.job_stream.ack(delivery.id)
                else:
                    self.job_stream.fail(delivery.id, str(exc))
                    self._defer_stream_delivery(session_id, delivery)
            return 1

        if completion is None:
            ledger_shadow.fail_and_dead_letter(
                self.duckdb_path,
                ledger_job_id,
                error_message="source generation superseded",
                error_category="summarizer_stale_generation",
            )
            if delivery is not None:
                self.job_stream.ack(delivery.id)
            return 1

        self._after_completion_commit()
        self._publish_downstream_jobs(
            session_id,
            source_version,
            completion,
        )
        if delivery is not None:
            self.job_stream.ack(delivery.id)
        return 1

    def _claim_duckdb_job(
        self,
    ) -> Optional[tuple[str, Optional[str], Optional[Delivery]]]:
        # Claim phase: retry on DuckDB optimistic-concurrency conflicts
        # ("Conflict on update!") that show up when multiple worker threads
        # race for the same row. Each retry re-reads to avoid stale state.
        for attempt in range(8):
            con = _open_summarizer_db(self.duckdb_path)
            try:
                now = self._db_now()
                con.execute(
                    """UPDATE summarize_jobs
                          SET status='pending', next_run_at=NULL, updated_at=?
                        WHERE status='retry_wait' AND next_run_at <= ?""",
                    [now, now],
                )
                row = con.execute(
                    """SELECT session_id, source_version FROM summarize_jobs
                       WHERE status='pending'
                       ORDER BY enqueued_at ASC LIMIT 1"""
                ).fetchone()
                if row is None:
                    return None
                candidate, source_version = row
                # Conditional update: only claim if still pending
                con.execute(
                    """UPDATE summarize_jobs
                       SET status='running', updated_at=?
                       WHERE session_id=? AND status='pending'""",
                    [now, candidate],
                )
                # If a sibling already claimed it, fetchone returns 0 affected rows.
                # DuckDB doesn't expose rowcount on UPDATE directly; verify via re-read.
                claimed = con.execute(
                    "SELECT status FROM summarize_jobs WHERE session_id=?",
                    [candidate],
                ).fetchone()
                if claimed and claimed[0] == "running":
                    return candidate, source_version, None
                # Else loop and try the next pending row.
            except duckdb.TransactionException:
                time.sleep(0.05 * (attempt + 1))
                continue
            finally:
                con.close()
        return None

    def _claim_stream_job(
        self,
    ) -> Optional[tuple[Optional[str], Optional[str], Optional[Delivery]]]:
        deliveries = self.job_stream.read_group(self.worker_id, count=1)
        if not deliveries:
            deliveries = self.job_stream.reclaim(self.worker_id, count=1)
        if not deliveries:
            return None
        delivery = deliveries[0]
        session_id = delivery.fields.get("session_id")
        if not session_id:
            self.job_stream.fail(delivery.id, "missing session_id")
            return None, None, delivery
        session_id = str(session_id)
        delivery_source_version = delivery.fields.get("source_version")
        if delivery_source_version is not None:
            delivery_source_version = str(delivery_source_version)

        for attempt in range(8):
            con = _open_summarizer_db(self.duckdb_path)
            try:
                row = con.execute(
                    """SELECT status, source_version, next_run_at
                         FROM summarize_jobs WHERE session_id=?""",
                    [session_id],
                ).fetchone()
                if row is None:
                    self.job_stream.ack(delivery.id)
                    return None, None, None
                status, source_version, next_run_at = row
                if delivery_source_version != source_version:
                    # Redis is delivery coordination, not durable truth. An old
                    # generation must never spend the current row's budget.
                    self.job_stream.ack(delivery.id)
                    return None, None, None
                if status in ("done", "dead_lettered"):
                    # Crash-after-write-before-ACK recovery: durable projection won,
                    # so the redelivery can be acknowledged without reprocessing.
                    self.job_stream.ack(delivery.id)
                    return None, None, None
                now = self._db_now()
                if status == "retry_wait":
                    if next_run_at is None or next_run_at > now:
                        if next_run_at is not None:
                            self._defer_delivery(delivery, next_run_at)
                        return None
                    con.execute(
                        """UPDATE summarize_jobs
                              SET status='pending', next_run_at=NULL, updated_at=?
                            WHERE session_id=? AND status='retry_wait'
                              AND source_version IS NOT DISTINCT FROM ?""",
                        [now, session_id, source_version],
                    )
                    status = "pending"
                if status != "pending":
                    return None
                con.execute(
                    """UPDATE summarize_jobs
                       SET status='running', updated_at=?
                       WHERE session_id=? AND status='pending'
                         AND source_version IS NOT DISTINCT FROM ?""",
                    [now, session_id, source_version],
                )
                claimed = con.execute(
                    "SELECT status FROM summarize_jobs WHERE session_id=?",
                    [session_id],
                ).fetchone()
                if claimed and claimed[0] == "running":
                    return session_id, source_version, delivery
            except duckdb.TransactionException:
                time.sleep(0.05 * (attempt + 1))
                continue
            finally:
                con.close()
        self.job_stream.fail(delivery.id, f"failed to claim summarize job {session_id}")
        return None, None, delivery

    def _finish_failure(
        self,
        session_id: str,
        source_version: Optional[str],
        exc: BaseException,
        *,
        ledger_job_id: Optional[str] = None,
    ) -> str:
        """Bound one generation's failures and mirror the result to the ledger."""
        self._before_failure_finish()
        con = _open_summarizer_db(self.duckdb_path)
        try:
            outcome = finish_summary_failure(
                con,
                session_id,
                source_version,
                str(exc),
                now=self._clock(),
                jitter=self._jitter,
            )
            if outcome == "stale":
                ledger_shadow.fail_and_dead_letter(
                    self.duckdb_path,
                    ledger_job_id,
                    error_message="source generation superseded",
                    error_category="summarizer_stale_generation",
                )
                return "stale"
            next_run_at = con.execute(
                """SELECT next_run_at FROM summarize_jobs
                     WHERE session_id=? AND source_version IS NOT DISTINCT FROM ?""",
                [session_id, source_version],
            ).fetchone()[0]
        finally:
            con.close()

        if ledger_job_id is None:
            ledger_job_id = ledger_shadow.begin_attempt(
                self.duckdb_path,
                job_kind="summarize_session",
                subject_key=session_id,
                subject_kind="session",
                worker_id="summarizer",
            )
        if outcome == "dead_lettered":
            ledger_shadow.fail_and_dead_letter(
                self.duckdb_path,
                ledger_job_id,
                error_message=str(exc),
                error_category="summarizer",
            )
        else:
            ledger_shadow.retry(
                self.duckdb_path,
                ledger_job_id,
                error_message=str(exc),
                next_run_at=next_run_at,
            )
        return outcome

    def _defer_stream_delivery(self, session_id: str, delivery: Delivery) -> None:
        con = _open_summarizer_db(self.duckdb_path)
        try:
            row = con.execute(
                "SELECT next_run_at FROM summarize_jobs WHERE session_id=?",
                [session_id],
            ).fetchone()
        finally:
            con.close()
        if row and row[0] is not None:
            self._defer_delivery(delivery, row[0])

    def _defer_delivery(self, delivery: Delivery, next_run_at: datetime) -> None:
        defer = getattr(self.job_stream, "defer", None)
        if defer is None:
            return
        due = next_run_at
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        defer(delivery.id, until_ms=int(due.timestamp() * 1000))

    def _publish_downstream_jobs(
        self,
        session_id: str,
        source_version: Optional[str],
        completion: _SummaryCompletion,
    ) -> None:
        """Publish durable downstream jobs after their fenced transaction commits."""
        try:
            if (
                self.embed_job_stream is not None
                and completion.embed_outcome in ("queued", "requeued")
                and self._embed_generation_is_current(session_id, source_version)
            ):
                self.embed_job_stream.add(
                    {"session_id": session_id, "source_version": source_version}
                )
        except Exception:  # noqa: BLE001
            log.exception(
                "embed publish for session %s failed (continuing)", session_id
            )
        try:
            if (
                self.brief_job_stream is not None
                and completion.project_key is not None
                and completion.brief_outcome in ("queued", "requeued")
                and self._brief_generation_is_current(
                    completion.project_key, session_id, source_version
                )
            ):
                self.brief_job_stream.add(
                    {
                        "project_key": completion.project_key,
                        "source_session_id": session_id,
                        "source_version": source_version,
                    }
                )
        except Exception:  # noqa: BLE001
            log.exception(
                "brief publish for session %s failed (continuing)", session_id
            )

    def _embed_generation_is_current(
        self, session_id: str, source_version: Optional[str]
    ) -> bool:
        con = _open_summarizer_db(self.duckdb_path)
        try:
            return (
                con.execute(
                    """SELECT 1 FROM embed_jobs e
                         JOIN summarize_jobs s USING (session_id)
                        WHERE e.session_id=? AND e.status='pending'
                          AND e.source_version IS NOT DISTINCT FROM ?
                          AND s.source_version IS NOT DISTINCT FROM ?
                          AND s.status='done'""",
                    [session_id, source_version, source_version],
                ).fetchone()
                is not None
            )
        finally:
            con.close()

    def _brief_generation_is_current(
        self,
        project_key: str,
        session_id: str,
        source_version: Optional[str],
    ) -> bool:
        con = _open_summarizer_db(self.duckdb_path)
        try:
            return (
                con.execute(
                    """SELECT 1 FROM brief_jobs b
                         JOIN summarize_jobs s ON s.session_id=b.source_session_id
                        WHERE b.project_key=? AND b.status='pending'
                          AND b.source_session_id=?
                          AND b.source_version IS NOT DISTINCT FROM ?
                          AND s.source_version IS NOT DISTINCT FROM ?
                          AND s.status='done'""",
                    [project_key, session_id, source_version, source_version],
                ).fetchone()
                is not None
            )
        finally:
            con.close()

    def _summarize_session(
        self,
        session_id: str,
        source_version: Optional[str],
        backend: Optional[LLMBackend] = None,
        *,
        ledger_job_id: Optional[str] = None,
    ) -> Optional[_SummaryCompletion]:
        con = _open_summarizer_db(self.duckdb_path)
        try:
            cur = con.execute(
                f"""WITH {_session_agent_events_ctes()}
                   SELECT id, timestamp, agent_id, event_type, role, content, raw_data
                   FROM canonical_agent_events
                   ORDER BY timestamp DESC LIMIT 30""",
                [session_id],
            )
            cols = [d[0] for d in cur.description]
            events_desc = [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            con.close()

        if not events_desc:
            raise RuntimeError(f"no events for session {session_id}")

        # Reverse to chronological order for the prompt
        events = list(reversed(events_desc))
        agent_id = events[-1].get("agent_id") or "unknown"

        files = compute_files_touched(events)
        tools = compute_tools_used(events)

        last_user = next(
            (e["content"] for e in reversed(events) if e.get("role") == "user"), ""
        )
        last_assistant = next(
            (e["content"] for e in reversed(events) if e.get("role") == "assistant"), ""
        )

        prompt = build_summary_prompt(
            events=[
                {
                    "role": e.get("role"),
                    "content": e.get("content"),
                    "timestamp": _iso(e.get("timestamp")),
                    "event_type": e.get("event_type"),
                }
                for e in events
            ],
            session_id=session_id,
            agent_id=agent_id,
            started_at=_iso(events[0].get("timestamp")),
            ended_at=_iso(events[-1].get("timestamp")),
        )

        if backend is not None:
            try:
                llm = backend.summarize(prompt)
            except BackendError as e:
                raise RuntimeError(str(e)) from e
            generator_model = backend.model
        else:
            try:
                llm = self._llm_call(prompt, api_key=self.api_key, model=self.model)
            except NoApiKeyError:
                raise RuntimeError("ANTHROPIC_API_KEY not configured (no_api_key)")
            generator_model = self.model

        # The test seam is deliberately before the single completion transaction:
        # any superseding generation either wins first and makes this stale, or is
        # ordered after all durable success effects commit together.
        self._before_success_effects()

        # Persist and create every durable success effect in one generation-fenced
        # transaction (retry on optimistic-concurrency conflicts).
        for attempt in range(8):
            con = _open_summarizer_db(self.duckdb_path)
            try:
                con.execute("BEGIN TRANSACTION")
                current = con.execute(
                    """SELECT 1 FROM summarize_jobs
                         WHERE session_id=? AND status='running'
                           AND source_version IS NOT DISTINCT FROM ?""",
                    [session_id, source_version],
                ).fetchone()
                if current is None:
                    con.execute("ROLLBACK")
                    return None
                con.execute(
                    """INSERT OR REPLACE INTO session_summaries
                       (session_id, task_id, agent_id, ended_at, summary_md,
                        files_touched, tools_used, last_user_prompt, last_assistant,
                        next_steps_md, open_questions, status, generator_model, generated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, now())""",
                    [
                        session_id,
                        events[0].get("raw_data") and _safe_task_id(con, session_id),
                        agent_id,
                        events[-1].get("timestamp"),
                        llm["summary_md"],
                        files,
                        tools,
                        (llm.get("last_user_prompt") or last_user or "")[-500:],
                        (llm.get("last_assistant") or last_assistant or "")[-500:],
                        llm["next_steps_md"],
                        llm.get("open_questions") or [],
                        generator_model,
                    ],
                )
                finalized = con.execute(
                    """UPDATE summarize_jobs
                          SET status='done', last_error=NULL, next_run_at=NULL,
                              dead_letter_streak=0, updated_at=now()
                        WHERE session_id=? AND status='running'
                          AND source_version IS NOT DISTINCT FROM ?
                        RETURNING session_id""",
                    [session_id, source_version],
                ).fetchone()
                if finalized is None:
                    con.execute("ROLLBACK")
                    return None
                project_row = con.execute(
                    f"""WITH {_session_agent_events_ctes()}
                       SELECT any_value(repo_owner), any_value(repo_name)
                       FROM canonical_agent_events
                       WHERE repo_owner IS NOT NULL AND repo_name IS NOT NULL""",
                    [session_id],
                ).fetchone()
                project_key = (
                    f"{project_row[0]}/{project_row[1]}"
                    if project_row and project_row[0] and project_row[1]
                    else None
                )
                brief_outcome = (
                    _enqueue_brief_on_connection(
                        con, project_key, session_id, source_version
                    )
                    if project_key is not None
                    else None
                )
                embed_outcome = _enqueue_embed_on_connection(
                    con, session_id, source_version
                )
                if ledger_job_id is not None:
                    Ledger(con).succeed_job(
                        ledger_job_id,
                        artifact=ArtifactSpec(
                            artifact_kind="session_summary",
                            subject_key=session_id,
                            storage_uri=f"session_summaries/{session_id}",
                            version_token=source_version,
                        ),
                    )
                con.execute("COMMIT")
                return _SummaryCompletion(
                    project_key=project_key,
                    brief_outcome=brief_outcome,
                    embed_outcome=embed_outcome,
                )
            except duckdb.TransactionException:
                try:
                    con.execute("ROLLBACK")
                except duckdb.Error:
                    pass
                time.sleep(0.05 * (attempt + 1))
            finally:
                con.close()
        raise RuntimeError(
            f"persist session_summaries for {session_id} failed after retries"
        )


def _iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


def _safe_task_id(con: duckdb.DuckDBPyConnection, session_id: str) -> Optional[str]:
    try:
        row = con.execute(
            f"""WITH {_session_agent_events_ctes()}
               SELECT any_value(task_id)
               FROM canonical_agent_events""",
            [session_id],
        ).fetchone()
        return row[0] if row else None
    except duckdb.Error:
        return None


def _enqueue_embed_on_connection(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    source_version: Optional[str],
) -> str:
    """Create/requeue the embed job on the caller's completion transaction."""
    existing = con.execute(
        "SELECT status, source_version FROM embed_jobs WHERE session_id=?", [session_id]
    ).fetchone()
    if existing:
        status, existing_version = existing
        if existing_version != source_version:
            con.execute(
                """UPDATE embed_jobs SET status='pending', attempts=0,
                          last_error=NULL, updated_at=now(), source_version=?
                     WHERE session_id=?""",
                [source_version, session_id],
            )
            return "requeued"
        if status == "done":
            return "already_done"
        if status in ("pending", "running"):
            return "already_queued"
        con.execute(
            """UPDATE embed_jobs
                  SET status='pending', last_error=NULL, updated_at=now()
                WHERE session_id=?""",
            [session_id],
        )
        return "requeued"
    con.execute(
        """INSERT INTO embed_jobs (session_id, status, attempts, source_version)
           VALUES (?, 'pending', 0, ?)""",
        [session_id, source_version],
    )
    return "queued"


def _enqueue_brief_on_connection(
    con: duckdb.DuckDBPyConnection,
    project_key: str,
    source_session_id: str,
    source_version: Optional[str],
) -> str:
    """Create/requeue the brief job on the caller's completion transaction."""
    existing = con.execute(
        """SELECT status, source_session_id, source_version
             FROM brief_jobs WHERE project_key=?""",
        [project_key],
    ).fetchone()
    if existing:
        status, existing_session_id, existing_version = existing
        if (existing_session_id, existing_version) != (
            source_session_id,
            source_version,
        ):
            con.execute(
                """UPDATE brief_jobs SET status='pending', attempts=0,
                          last_error=NULL, updated_at=now(),
                          source_session_id=?, source_version=?
                     WHERE project_key=?""",
                [source_session_id, source_version, project_key],
            )
            return "requeued"
        if status in ("pending", "running"):
            return "already_queued"
        con.execute(
            """UPDATE brief_jobs
                  SET status='pending', last_error=NULL, updated_at=now()
                WHERE project_key=?""",
            [project_key],
        )
        return "requeued"
    con.execute(
        """INSERT INTO brief_jobs
             (project_key, status, attempts, source_session_id, source_version)
           VALUES (?, 'pending', 0, ?, ?)""",
        [project_key, source_session_id, source_version],
    )
    return "queued"
