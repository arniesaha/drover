"""SummarizerWorker — drains summarize_jobs into session_summaries.

Designed for both threaded poll-loop use (start/stop) and one-shot
drain (drain_once for tests / explicit catch-up runs).

Each pending job:
  1. Mark status='running' (atomic update).
  2. Read session events from agent_events.
  3. Compute deterministic fields (files_touched, tools_used).
  4. Build prompt → call backend → parse JSON.
  5. INSERT INTO session_summaries.
  6. Mark status='done'.
On any exception: status='errored', last_error captured, attempts++.

Backend selection prefers Anthropic whenever credentials are functional and
falls back to the local Ollama GPU rig only when Anthropic is unavailable.
See ``backends.select_backend``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import duckdb

from drover.event_identity import canonical_agent_events_cte
from drover.server.jobs import Delivery
from drover.server import ledger_shadow
from drover.server.db import open_duckdb_connection
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
from drover.server.summarizer.prompt import build_summary_prompt

log = logging.getLogger("drover.summarizer.worker")


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
            return self._drain_stream_batch(max_jobs=max_jobs)

        # Check for pending jobs BEFORE selecting a backend so that backend
        # fallback warnings are never emitted on idle poll ticks (fixes #55).
        con = _open_summarizer_db(self.duckdb_path)
        try:
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

    def _drain_stream_batch(self, *, max_jobs: int) -> int:
        """Drain stream deliveries without warming the backend on idle ticks."""
        processed = 0
        backend: Optional[LLMBackend] = None
        backend_checked = False
        for _ in range(max_jobs):
            claim = self._claim_stream_job()
            if claim is None:
                break
            session_id, delivery = claim
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
                session_id=session_id, delivery=delivery, backend=backend
            )
        return processed

    def _drain_one(self, backend: Optional[LLMBackend]) -> int:
        claim = (
            self._claim_stream_job() if self.job_stream else self._claim_duckdb_job()
        )
        if claim is None:
            return 0
        session_id, delivery = claim
        if session_id is None:
            return 1
        return self._process_claim(
            session_id=session_id, delivery=delivery, backend=backend
        )

    def _process_claim(
        self,
        *,
        session_id: str,
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
            self._summarize_session(session_id, backend)
        except Exception as exc:  # noqa: BLE001
            log.warning("summarize %s failed: %s", session_id, exc)
            self._mark_errored(session_id, str(exc))
            if delivery is not None:
                self.job_stream.fail(delivery.id, str(exc))
            ledger_shadow.retry(self.duckdb_path, ledger_job_id, error_message=str(exc))
            return 1

        self._mark_done(session_id)
        ledger_shadow.succeed(
            self.duckdb_path,
            ledger_job_id,
            artifact_kind="session_summary",
            subject_key=session_id,
            storage_uri=f"session_summaries/{session_id}",
        )
        self._maybe_enqueue_brief(session_id)
        self._maybe_enqueue_embed(session_id)
        if delivery is not None:
            self.job_stream.ack(delivery.id)
        return 1

    def _claim_duckdb_job(self) -> Optional[tuple[str, Optional[Delivery]]]:
        # Claim phase: retry on DuckDB optimistic-concurrency conflicts
        # ("Conflict on update!") that show up when multiple worker threads
        # race for the same row. Each retry re-reads to avoid stale state.
        for attempt in range(8):
            con = _open_summarizer_db(self.duckdb_path)
            try:
                row = con.execute("""SELECT session_id, attempts FROM summarize_jobs
                       WHERE status='pending'
                       ORDER BY enqueued_at ASC LIMIT 1""").fetchone()
                if row is None:
                    return None
                candidate, attempts = row[0], row[1] or 0
                # Conditional update: only claim if still pending
                con.execute(
                    """UPDATE summarize_jobs
                       SET status='running', attempts=?, updated_at=now()
                       WHERE session_id=? AND status='pending'""",
                    [attempts + 1, candidate],
                )
                # If a sibling already claimed it, fetchone returns 0 affected rows.
                # DuckDB doesn't expose rowcount on UPDATE directly; verify via re-read.
                claimed = con.execute(
                    "SELECT status FROM summarize_jobs WHERE session_id=?",
                    [candidate],
                ).fetchone()
                if claimed and claimed[0] == "running":
                    return candidate, None
                # Else loop and try the next pending row.
            except duckdb.TransactionException:
                time.sleep(0.05 * (attempt + 1))
                continue
            finally:
                con.close()
        return None

    def _claim_stream_job(self) -> Optional[tuple[Optional[str], Optional[Delivery]]]:
        deliveries = self.job_stream.read_group(self.worker_id, count=1)
        if not deliveries:
            deliveries = self.job_stream.reclaim(self.worker_id, count=1)
        if not deliveries:
            return None
        delivery = deliveries[0]
        session_id = delivery.fields.get("session_id")
        if not session_id:
            self.job_stream.fail(delivery.id, "missing session_id")
            return None, delivery
        session_id = str(session_id)

        for attempt in range(8):
            con = _open_summarizer_db(self.duckdb_path)
            try:
                row = con.execute(
                    "SELECT status, attempts FROM summarize_jobs WHERE session_id=?",
                    [session_id],
                ).fetchone()
                if row is None:
                    con.execute(
                        """INSERT INTO summarize_jobs
                           (session_id, status, attempts, updated_at)
                           VALUES (?, 'running', 1, now())""",
                        [session_id],
                    )
                    return session_id, delivery
                status, attempts = row[0], row[1] or 0
                if status == "done":
                    # Crash-after-write-before-ACK recovery: durable projection won,
                    # so the redelivery can be acknowledged without reprocessing.
                    self.job_stream.ack(delivery.id)
                    return None, None
                con.execute(
                    """UPDATE summarize_jobs
                       SET status='running', attempts=?, updated_at=now()
                       WHERE session_id=?""",
                    [attempts + 1, session_id],
                )
                return session_id, delivery
            except duckdb.TransactionException:
                time.sleep(0.05 * (attempt + 1))
                continue
            finally:
                con.close()
        self.job_stream.fail(delivery.id, f"failed to claim summarize job {session_id}")
        return None, delivery

    def _maybe_enqueue_embed(self, session_id: str) -> None:
        """Enqueue an embed_job for the just-summarized session. Best-effort."""
        try:
            from drover.server.embeddings.worker import enqueue_embed

            outcome = enqueue_embed(self.duckdb_path, session_id)
            if self.embed_job_stream is not None and outcome in ("queued", "requeued"):
                self.embed_job_stream.add({"session_id": session_id})
            log.debug("embed enqueue %s → %s", session_id, outcome)
        except Exception:  # noqa: BLE001
            log.exception(
                "embed enqueue for session %s failed (continuing)", session_id
            )

    def _maybe_enqueue_brief(self, session_id: str) -> None:
        """If the session has a (repo_owner, repo_name), enqueue a brief regen.

        Best-effort: failures are logged and swallowed so the summarize itself
        is not marked errored.
        """
        try:
            # Read-write connection to match sibling threads' connection mode
            # (DuckDB rejects mixing read-only and read-write conns to the same file).
            con = _open_summarizer_db(self.duckdb_path)
            try:
                row = con.execute(
                    f"""WITH {_session_agent_events_ctes()}
                       SELECT any_value(repo_owner), any_value(repo_name)
                       FROM canonical_agent_events
                       WHERE repo_owner IS NOT NULL AND repo_name IS NOT NULL""",
                    [session_id],
                ).fetchone()
            finally:
                con.close()
            if not row or not row[0] or not row[1]:
                return
            project_key = f"{row[0]}/{row[1]}"
            from drover.server.briefs.worker import enqueue_brief

            outcome = enqueue_brief(self.duckdb_path, project_key)
            if self.brief_job_stream is not None and outcome in ("queued", "requeued"):
                self.brief_job_stream.add({"project_key": project_key})
            log.debug("brief enqueue %s → %s", project_key, outcome)
        except Exception:  # noqa: BLE001
            log.exception(
                "brief enqueue for session %s failed (continuing)", session_id
            )

    def _summarize_session(
        self, session_id: str, backend: Optional[LLMBackend] = None
    ) -> None:
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

        # Persist (retry on optimistic-concurrency conflicts)
        for attempt in range(8):
            con = _open_summarizer_db(self.duckdb_path)
            try:
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
                return
            except duckdb.TransactionException:
                time.sleep(0.05 * (attempt + 1))
            finally:
                con.close()
        raise RuntimeError(
            f"persist session_summaries for {session_id} failed after retries"
        )

    def _mark_done(self, session_id: str) -> None:
        self._update_status(session_id, "done", error=None)

    def _mark_errored(self, session_id: str, message: str) -> None:
        self._update_status(session_id, "errored", error=message)

    def _update_status(
        self, session_id: str, status: str, *, error: Optional[str]
    ) -> None:
        # Retry on optimistic-concurrency conflicts; finalizing a job is critical.
        for attempt in range(8):
            con = _open_summarizer_db(self.duckdb_path)
            try:
                con.execute(
                    "UPDATE summarize_jobs SET status=?, last_error=?, updated_at=now() WHERE session_id=?",
                    [status, error, session_id],
                )
                return
            except duckdb.TransactionException:
                time.sleep(0.05 * (attempt + 1))
            finally:
                con.close()
        log.error("failed to mark %s as %s after retries", session_id, status)


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
