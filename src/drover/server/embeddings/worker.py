"""EmbedWorker — drains embedding jobs into vector tables.

Session summaries and raw spans have different source semantics, so they are
stored separately:

* ``embed_jobs`` -> ``session_embeddings`` embeds ``session_summaries.summary_md``
  keyed by ``session_id``.
* ``span_embed_jobs`` -> ``span_embeddings`` embeds a redacted/truncated text
  rendering of selected ``spans`` fields keyed by ``span_id``.

The same configured API-first embedding backend is used for both queues. The
local Ollama/GPU path remains a fallback for offline/no-API operation.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import duckdb

from drover.server import ledger_shadow
from drover.server.db import open_duckdb_connection
from drover.server.embeddings.client import (
    DEFAULT_EMBED_MODEL,
    EmbeddingBackendConfig,
    OllamaEmbedder,
)
from drover.server.summarizer.backends import SummarizerBackendConfig
from drover.server.summarizer.backends.types import BackendError

log = logging.getLogger("drover.embeddings.worker")

SPAN_EMBED_TEXT_FIELDS = (
    "name",
    "project",
    "task_label",
    "activity_type",
    "prompt_preview",
    "response_preview",
)
SPAN_EMBED_MAX_CHARS = 4096

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{6,}"),
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _redact_text(value: str) -> str:
    value = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED_SECRET]", value)
    return value


def build_span_embedding_text(
    row: dict, *, max_chars: int = SPAN_EMBED_MAX_CHARS
) -> str:
    """Render the embeddable text for a span with redaction and truncation.

    Embeddable fields are intentionally limited to:
    ``name``, ``project``, ``task_label``, ``activity_type``,
    ``prompt_preview``, and ``response_preview``. The preview fields are already
    clipped by ingest; this function applies a second bounded-source guard and
    strips common emails/tokens/secrets before persistence or embedding.
    """
    parts: list[str] = []
    labels = {
        "prompt_preview": "prompt",
        "response_preview": "response",
    }
    for field in SPAN_EMBED_TEXT_FIELDS:
        value = row.get(field)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        parts.append(f"{labels.get(field, field)}: {_redact_text(text)}")
    rendered = "\n".join(parts)
    if len(rendered) > max_chars:
        suffix = "...[truncated]"
        rendered = rendered[: max(0, max_chars - len(suffix))].rstrip() + suffix
    return rendered


class EmbedWorker:
    def __init__(
        self,
        *,
        duckdb_path: Path,
        embedder: Optional[object] = None,
        backend_config: Optional[SummarizerBackendConfig] = None,
        embedding_config: Optional[EmbeddingBackendConfig] = None,
        embed_model: str = DEFAULT_EMBED_MODEL,
        batch_size: int = 16,
        poll_interval_s: float = 30.0,
        session_job_stream: Optional[object] = None,
        span_job_stream: Optional[object] = None,
        worker_id: str = "embeddings",
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self._embedder = embedder
        self._backend_config = backend_config
        self._embedding_config = embedding_config
        self._resolved_embedder: Optional[object] = None
        self.embed_model = embed_model
        self.batch_size = max(1, int(batch_size))
        self.poll_interval_s = poll_interval_s
        self.session_job_stream = session_job_stream
        self.span_job_stream = span_job_stream
        self.worker_id = worker_id
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _resolve_embedder(self) -> Optional[object]:
        if self._embedder is not None:
            return self._embedder
        if self._resolved_embedder is not None:
            return self._resolved_embedder
        if self._embedding_config is not None:
            self._resolved_embedder = self._embedding_config.select_embedder()
            return self._resolved_embedder
        if self._backend_config is None or self._backend_config.gpu_rig is None:
            return None
        self._resolved_embedder = EmbeddingBackendConfig.from_runtime(
            gpu_rig=self._backend_config.gpu_rig,
            local_model=self.embed_model,
        ).select_embedder()
        return self._resolved_embedder

    def start(self) -> None:
        if self._thread is not None:
            return
        # Crash recovery (AGE-45): reconcile crashed in-flight work from DuckDB
        # for both session- and span-embed kinds before draining.
        for job_kind in ("embed_session", "embed_span"):
            ledger_shadow.recover_runnable(self.duckdb_path, job_kind=job_kind)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="drover-embeddings", daemon=True
        )
        self._thread.start()
        log.info("embed worker started")

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_batch()
            except Exception:  # noqa: BLE001
                log.exception("embed drain loop crashed (will retry)")
            self._stop.wait(self.poll_interval_s)

    def drain_batch(self, *, max_jobs: Optional[int] = None) -> int:
        if max_jobs is None:
            max_jobs = self.batch_size
        session_rows = self._claim_session_jobs(max_jobs=max_jobs)
        remaining = max(0, max_jobs - len(session_rows))
        span_rows = self._claim_span_jobs(max_jobs=remaining)
        if not session_rows and not span_rows:
            return 0

        embedder = self._resolve_embedder()
        if embedder is None:
            log.debug(
                "embed worker: no embedder configured (no GPU rig); leaving jobs pending"
            )
            self._release_session_jobs(session_rows)
            self._release_span_jobs(span_rows)
            self._fail_session_deliveries(session_rows, "no embedder configured")
            self._fail_span_deliveries(span_rows, "no embedder configured")
            return 0
        try:
            embedder.ensure_ready()
        except Exception as e:  # noqa: BLE001 - readiness can fail below BackendError.
            log.warning("embed ensure_ready failed: %s", e)
            self._release_session_jobs(session_rows)
            self._release_span_jobs(span_rows)
            self._fail_session_deliveries(session_rows, str(e))
            self._fail_span_deliveries(span_rows, str(e))
            return 0

        processed = 0
        if session_rows:
            texts = [r["summary_md"] or "" for r in session_rows]
            try:
                vectors = embedder.embed_batch(texts)
            except BackendError as e:
                log.warning("session embed batch failed: %s", e)
                for r in session_rows:
                    self._mark_errored(r["session_id"], str(e))
                    self._fail_session_delivery(r, str(e))
                    ledger_shadow.retry(
                        self.duckdb_path,
                        r.get("_ledger_job_id"),
                        error_message=str(e),
                    )
                processed += len(session_rows)
            else:
                for r, v in zip(session_rows, vectors):
                    try:
                        self._persist(r["session_id"], v, embedder.model)
                        self._mark_done(r["session_id"])
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "persist embedding for %s failed: %s", r["session_id"], exc
                        )
                        self._mark_errored(r["session_id"], str(exc))
                        self._fail_session_delivery(r, str(exc))
                        ledger_shadow.retry(
                            self.duckdb_path,
                            r.get("_ledger_job_id"),
                            error_message=str(exc),
                        )
                    else:
                        ledger_shadow.succeed(
                            self.duckdb_path,
                            r.get("_ledger_job_id"),
                            artifact_kind="session_embedding",
                            subject_key=r["session_id"],
                            version_token=embedder.model,
                        )
                        self._ack_session_delivery(r)
                processed += len(session_rows)

        if span_rows:
            texts = [r["source_text"] for r in span_rows]
            try:
                vectors = embedder.embed_batch(texts)
            except BackendError as e:
                log.warning("span embed batch failed: %s", e)
                for r in span_rows:
                    self._mark_span_errored(r["span_id"], str(e))
                    self._fail_span_delivery(r, str(e))
                    ledger_shadow.retry(
                        self.duckdb_path,
                        r.get("_ledger_job_id"),
                        error_message=str(e),
                    )
                processed += len(span_rows)
            else:
                for r, v in zip(span_rows, vectors):
                    try:
                        self._persist_span(r, v, embedder.model)
                        self._mark_span_done(r["span_id"])
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "persist span embedding for %s failed: %s",
                            r["span_id"],
                            exc,
                        )
                        self._mark_span_errored(r["span_id"], str(exc))
                        self._fail_span_delivery(r, str(exc))
                        ledger_shadow.retry(
                            self.duckdb_path,
                            r.get("_ledger_job_id"),
                            error_message=str(exc),
                        )
                    else:
                        ledger_shadow.succeed(
                            self.duckdb_path,
                            r.get("_ledger_job_id"),
                            artifact_kind="span_embedding",
                            subject_key=r["span_id"],
                            version_token=embedder.model,
                        )
                        self._ack_span_delivery(r)
                processed += len(span_rows)

        return processed

    def _claim_session_jobs(self, *, max_jobs: int) -> list[dict]:
        if self.session_job_stream is not None:
            return self._claim_session_stream_jobs(max_jobs=max_jobs)

        con = open_duckdb_connection(self.duckdb_path)
        try:
            cur = con.execute(
                """SELECT j.session_id, ss.summary_md
                   FROM embed_jobs j
                   JOIN session_summaries ss USING (session_id)
                   WHERE j.status='pending'
                     AND (j.source_version IS NULL OR EXISTS (
                       SELECT 1 FROM summarize_jobs s
                        WHERE s.session_id=j.session_id
                          AND s.source_version IS NOT DISTINCT FROM j.source_version
                          AND s.status='done'
                     ))
                   ORDER BY j.enqueued_at ASC
                   LIMIT ?""",
                [max_jobs],
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            claimed_rows: list[dict] = []
            for r in rows:
                claimed = con.execute(
                    """UPDATE embed_jobs j SET status='running',
                       attempts = COALESCE(attempts,0)+1, updated_at=now()
                       WHERE j.session_id=? AND j.status='pending'
                         AND (j.source_version IS NULL OR EXISTS (
                           SELECT 1 FROM summarize_jobs s
                            WHERE s.session_id=j.session_id
                              AND s.source_version IS NOT DISTINCT FROM j.source_version
                              AND s.status='done'
                         ))
                       RETURNING j.session_id""",
                    [r["session_id"]],
                ).fetchone()
                if claimed is not None:
                    claimed_rows.append(r)
            rows = claimed_rows
        finally:
            con.close()
        # Shadow durable-ledger lease per claimed session job (AGE-44).
        for r in rows:
            r["_ledger_job_id"] = ledger_shadow.begin_attempt(
                self.duckdb_path,
                job_kind="embed_session",
                subject_key=r["session_id"],
                subject_kind="session",
                worker_id="embeddings",
            )
        return rows

    def _claim_span_jobs(self, *, max_jobs: int) -> list[dict]:
        if max_jobs <= 0:
            return []
        if self.span_job_stream is not None:
            return self._claim_span_stream_jobs(max_jobs=max_jobs)

        con = open_duckdb_connection(self.duckdb_path)
        try:
            pending = con.execute(
                """SELECT span_id
                   FROM span_embed_jobs
                   WHERE status='pending'
                   ORDER BY enqueued_at ASC
                   LIMIT ?""",
                [max_jobs],
            ).fetchall()
            span_ids = [row[0] for row in pending]
            if not span_ids:
                return []

            placeholders = ", ".join("?" for _ in span_ids)
            cur = con.execute(
                f"""SELECT s.span_id, s.trace_id, s.session_id, s.task_id, s.agent_id,
                          s.repo_owner, s.repo_name, s.branch,
                          s.name, s.project, s.task_label, s.activity_type,
                          s.prompt_preview, s.response_preview
                   FROM spans s
                   WHERE s.span_id IN ({placeholders})""",
                span_ids,
            )
            cols = [d[0] for d in cur.description]
            rows_by_id = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
            out: list[dict] = []
            for span_id in span_ids:
                r = rows_by_id.get(span_id)
                if r is None:
                    con.execute(
                        """UPDATE span_embed_jobs SET status='errored',
                           last_error='span row missing', updated_at=now()
                           WHERE span_id=?""",
                        [span_id],
                    )
                    continue
                source_text = build_span_embedding_text(r)
                if not source_text:
                    con.execute(
                        """UPDATE span_embed_jobs SET status='errored',
                           last_error='no embeddable span text', updated_at=now()
                           WHERE span_id=?""",
                        [r["span_id"]],
                    )
                    continue
                r["source_text"] = source_text
                r["source_fields"] = [
                    f for f in SPAN_EMBED_TEXT_FIELDS if r.get(f) not in (None, "")
                ]
                out.append(r)
                con.execute(
                    """UPDATE span_embed_jobs SET status='running',
                       attempts = COALESCE(attempts,0)+1, updated_at=now()
                       WHERE span_id=?""",
                    [r["span_id"]],
                )
        finally:
            con.close()
        # Shadow durable-ledger lease per claimed span job (AGE-44).
        for r in out:
            r["_ledger_job_id"] = ledger_shadow.begin_attempt(
                self.duckdb_path,
                job_kind="embed_span",
                subject_key=r["span_id"],
                subject_kind="span",
                worker_id="embeddings",
            )
        return out

    def _claim_session_stream_jobs(self, *, max_jobs: int) -> list[dict]:
        deliveries = self.session_job_stream.read_group(self.worker_id, count=max_jobs)
        if len(deliveries) < max_jobs:
            deliveries.extend(
                self.session_job_stream.reclaim(
                    self.worker_id, count=max_jobs - len(deliveries)
                )
            )
        if not deliveries:
            return []

        rows: list[dict] = []
        con = open_duckdb_connection(self.duckdb_path)
        try:
            for delivery in deliveries:
                session_id = delivery.fields.get("session_id")
                if not session_id:
                    self.session_job_stream.fail(delivery.id, "missing session_id")
                    continue
                session_id = str(session_id)
                delivery_source_version = delivery.fields.get("source_version")
                if delivery_source_version is not None:
                    delivery_source_version = str(delivery_source_version)
                job = con.execute(
                    """SELECT status, attempts, source_version
                         FROM embed_jobs WHERE session_id=?""",
                    [session_id],
                ).fetchone()
                if delivery_source_version is not None:
                    if job is None:
                        self.session_job_stream.ack(delivery.id)
                        continue
                    claimed = con.execute(
                        """UPDATE embed_jobs e
                              SET status='running',
                                  attempts=COALESCE(attempts, 0)+1,
                                  updated_at=now()
                            WHERE e.session_id=?
                              AND e.status NOT IN ('done', 'superseded')
                              AND e.source_version IS NOT DISTINCT FROM ?
                              AND EXISTS (
                                SELECT 1 FROM summarize_jobs s
                                 WHERE s.session_id=e.session_id
                                   AND s.source_version IS NOT DISTINCT FROM e.source_version
                                   AND s.status='done'
                              )
                            RETURNING e.session_id""",
                        [session_id, delivery_source_version],
                    ).fetchone()
                    if claimed is None:
                        self.session_job_stream.ack(delivery.id)
                        continue
                    summary = con.execute(
                        "SELECT summary_md FROM session_summaries WHERE session_id=?",
                        [session_id],
                    ).fetchone()
                    if summary is None:
                        con.execute(
                            """UPDATE embed_jobs SET status='errored',
                                      last_error='session summary missing', updated_at=now()
                                 WHERE session_id=?""",
                            [session_id],
                        )
                        self.session_job_stream.fail(
                            delivery.id, "session summary missing"
                        )
                        continue
                    rows.append(
                        {
                            "session_id": session_id,
                            "summary_md": summary[0],
                            "_delivery": delivery,
                        }
                    )
                    continue
                if job and job[0] == "done":
                    self.session_job_stream.ack(delivery.id)
                    continue
                summary = con.execute(
                    "SELECT summary_md FROM session_summaries WHERE session_id=?",
                    [session_id],
                ).fetchone()
                if summary is None:
                    if job is None:
                        con.execute(
                            """INSERT INTO embed_jobs
                               (session_id, status, attempts, last_error, updated_at)
                               VALUES (?, 'errored', 1, 'session summary missing', now())""",
                            [session_id],
                        )
                    else:
                        con.execute(
                            """UPDATE embed_jobs SET status='errored',
                               attempts=COALESCE(attempts,0)+1,
                               last_error='session summary missing',
                               updated_at=now()
                               WHERE session_id=?""",
                            [session_id],
                        )
                    self.session_job_stream.fail(delivery.id, "session summary missing")
                    continue
                attempts = (job[1] if job else 0) or 0
                if job is None:
                    con.execute(
                        """INSERT INTO embed_jobs
                           (session_id, status, attempts, updated_at)
                           VALUES (?, 'running', 1, now())""",
                        [session_id],
                    )
                else:
                    con.execute(
                        """UPDATE embed_jobs SET status='running',
                           attempts=?, updated_at=now()
                           WHERE session_id=?""",
                        [attempts + 1, session_id],
                    )
                rows.append(
                    {
                        "session_id": session_id,
                        "summary_md": summary[0],
                        "_delivery": delivery,
                    }
                )
        finally:
            con.close()

        for r in rows:
            r["_ledger_job_id"] = ledger_shadow.begin_attempt(
                self.duckdb_path,
                job_kind="embed_session",
                subject_key=r["session_id"],
                subject_kind="session",
                worker_id="embeddings",
            )
        return rows

    def _claim_span_stream_jobs(self, *, max_jobs: int) -> list[dict]:
        deliveries = self.span_job_stream.read_group(self.worker_id, count=max_jobs)
        if len(deliveries) < max_jobs:
            deliveries.extend(
                self.span_job_stream.reclaim(
                    self.worker_id, count=max_jobs - len(deliveries)
                )
            )
        if not deliveries:
            return []

        out: list[dict] = []
        con = open_duckdb_connection(self.duckdb_path)
        try:
            for delivery in deliveries:
                span_id = delivery.fields.get("span_id")
                if not span_id:
                    self.span_job_stream.fail(delivery.id, "missing span_id")
                    continue
                span_id = str(span_id)
                job = con.execute(
                    "SELECT status, attempts FROM span_embed_jobs WHERE span_id=?",
                    [span_id],
                ).fetchone()
                if job and job[0] == "done":
                    self.span_job_stream.ack(delivery.id)
                    continue
                cur = con.execute(
                    """SELECT s.span_id, s.trace_id, s.session_id, s.task_id, s.agent_id,
                              s.repo_owner, s.repo_name, s.branch,
                              s.name, s.project, s.task_label, s.activity_type,
                              s.prompt_preview, s.response_preview
                       FROM spans s
                       WHERE s.span_id=?""",
                    [span_id],
                )
                cols = [d[0] for d in cur.description]
                row = cur.fetchone()
                if row is None:
                    self._upsert_span_stream_error(
                        con, span_id, "span row missing", job
                    )
                    self.span_job_stream.fail(delivery.id, "span row missing")
                    continue
                r = dict(zip(cols, row))
                source_text = build_span_embedding_text(r)
                if not source_text:
                    self._upsert_span_stream_error(
                        con, span_id, "no embeddable span text", job
                    )
                    self.span_job_stream.fail(delivery.id, "no embeddable span text")
                    continue
                attempts = (job[1] if job else 0) or 0
                if job is None:
                    con.execute(
                        """INSERT INTO span_embed_jobs
                           (span_id, status, attempts, updated_at)
                           VALUES (?, 'running', 1, now())""",
                        [span_id],
                    )
                else:
                    con.execute(
                        """UPDATE span_embed_jobs SET status='running',
                           attempts=?, updated_at=now()
                           WHERE span_id=?""",
                        [attempts + 1, span_id],
                    )
                r["source_text"] = source_text
                r["source_fields"] = [
                    f for f in SPAN_EMBED_TEXT_FIELDS if r.get(f) not in (None, "")
                ]
                r["_delivery"] = delivery
                out.append(r)
        finally:
            con.close()

        for r in out:
            r["_ledger_job_id"] = ledger_shadow.begin_attempt(
                self.duckdb_path,
                job_kind="embed_span",
                subject_key=r["span_id"],
                subject_kind="span",
                worker_id="embeddings",
            )
        return out

    def _upsert_span_stream_error(
        self,
        con: duckdb.DuckDBPyConnection,
        span_id: str,
        message: str,
        job: Optional[tuple],
    ) -> None:
        if job is None:
            con.execute(
                """INSERT INTO span_embed_jobs
                   (span_id, status, attempts, last_error, updated_at)
                   VALUES (?, 'errored', 1, ?, now())""",
                [span_id, message],
            )
        else:
            con.execute(
                """UPDATE span_embed_jobs SET status='errored',
                   attempts=COALESCE(attempts,0)+1,
                   last_error=?, updated_at=now()
                   WHERE span_id=?""",
                [message, span_id],
            )

    def _release_session_jobs(self, rows: list[dict]) -> None:
        if not rows:
            return
        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.executemany(
                """UPDATE embed_jobs SET status='pending', updated_at=now()
                   WHERE session_id=? AND status='running'""",
                [(r["session_id"],) for r in rows],
            )
        finally:
            con.close()

    def _release_span_jobs(self, rows: list[dict]) -> None:
        if not rows:
            return
        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.executemany(
                """UPDATE span_embed_jobs SET status='pending', updated_at=now()
                   WHERE span_id=? AND status='running'""",
                [(r["span_id"],) for r in rows],
            )
        finally:
            con.close()

    def _ack_session_delivery(self, row: dict) -> None:
        delivery = row.get("_delivery")
        if delivery is not None:
            self.session_job_stream.ack(delivery.id)

    def _fail_session_delivery(self, row: dict, message: str) -> None:
        delivery = row.get("_delivery")
        if delivery is not None:
            self.session_job_stream.fail(delivery.id, message)

    def _fail_session_deliveries(self, rows: list[dict], message: str) -> None:
        for row in rows:
            self._fail_session_delivery(row, message)

    def _ack_span_delivery(self, row: dict) -> None:
        delivery = row.get("_delivery")
        if delivery is not None:
            self.span_job_stream.ack(delivery.id)

    def _fail_span_delivery(self, row: dict, message: str) -> None:
        delivery = row.get("_delivery")
        if delivery is not None:
            self.span_job_stream.fail(delivery.id, message)

    def _fail_span_deliveries(self, rows: list[dict], message: str) -> None:
        for row in rows:
            self._fail_span_delivery(row, message)

    def _persist(self, session_id: str, vector: list[float], model: str) -> None:
        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.execute(
                """INSERT OR REPLACE INTO session_embeddings
                   (session_id, embedding, model, dim, embedded_at)
                   VALUES (?, ?, ?, ?, now())""",
                [session_id, vector, model, len(vector)],
            )
        finally:
            con.close()

    def _persist_span(self, row: dict, vector: list[float], model: str) -> None:
        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.execute(
                """INSERT OR REPLACE INTO span_embeddings
                   (span_id, trace_id, session_id, task_id, agent_id,
                    repo_owner, repo_name, branch, source_text, source_fields,
                    embedding, model, dim, embedded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())""",
                [
                    row["span_id"],
                    row.get("trace_id"),
                    row.get("session_id"),
                    row.get("task_id"),
                    row.get("agent_id"),
                    row.get("repo_owner"),
                    row.get("repo_name"),
                    row.get("branch"),
                    row["source_text"],
                    row["source_fields"],
                    vector,
                    model,
                    len(vector),
                ],
            )
        finally:
            con.close()

    def _mark_done(self, session_id: str) -> None:
        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.execute(
                "UPDATE embed_jobs SET status='done', last_error=NULL, updated_at=now() WHERE session_id=?",
                [session_id],
            )
        finally:
            con.close()

    def _mark_errored(self, session_id: str, message: str) -> None:
        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.execute(
                "UPDATE embed_jobs SET status='errored', last_error=?, updated_at=now() WHERE session_id=?",
                [message, session_id],
            )
        finally:
            con.close()

    def _mark_span_done(self, span_id: str) -> None:
        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.execute(
                "UPDATE span_embed_jobs SET status='done', last_error=NULL, updated_at=now() WHERE span_id=?",
                [span_id],
            )
        finally:
            con.close()

    def _mark_span_errored(self, span_id: str, message: str) -> None:
        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.execute(
                "UPDATE span_embed_jobs SET status='errored', last_error=?, updated_at=now() WHERE span_id=?",
                [message, span_id],
            )
        finally:
            con.close()


def enqueue_embed(duckdb_path: Path, session_id: str) -> str:
    """Idempotent insert into embed_jobs. Returns 'queued'|'already_queued'|'requeued'|'already_done'."""
    con = open_duckdb_connection(duckdb_path)
    try:
        existing = con.execute(
            "SELECT status FROM embed_jobs WHERE session_id=?",
            [session_id],
        ).fetchone()
        if existing:
            status = existing[0]
            if status == "done":
                return "already_done"
            if status in ("pending", "running"):
                return "already_queued"
            con.execute(
                "UPDATE embed_jobs SET status='pending', last_error=NULL, updated_at=now() WHERE session_id=?",
                [session_id],
            )
            return "requeued"
        con.execute(
            "INSERT INTO embed_jobs (session_id, status, attempts) VALUES (?, 'pending', 0)",
            [session_id],
        )
    finally:
        con.close()
    return "queued"


def reset_stale_session_embed_jobs(
    *,
    duckdb_path: Path,
    stale_after_hours: int = 24,
    limit: int = 1000,
    apply: bool = False,
) -> dict:
    """Safely reset stranded running session embed jobs back to pending.

    Dry-run by default. Only ``running`` jobs whose ``updated_at`` (or
    ``enqueued_at`` when ``updated_at`` is null) is older than the threshold are
    matched; fresh running jobs are left untouched.
    """
    stale_after_hours = max(1, int(stale_after_hours))
    limit = max(1, int(limit))
    con = open_duckdb_connection(duckdb_path, read_only=not apply, role="diagnostic")
    try:
        rows = con.execute(
            """
            SELECT session_id
              FROM embed_jobs
             WHERE status = 'running'
               AND COALESCE(updated_at, enqueued_at) < now() - (? * INTERVAL '1 hour')
             ORDER BY COALESCE(updated_at, enqueued_at) ASC NULLS FIRST, session_id
             LIMIT ?
            """,
            [stale_after_hours, limit],
        ).fetchall()
        session_ids = [str(row[0]) for row in rows]
        if apply and session_ids:
            con.executemany(
                """
                UPDATE embed_jobs
                   SET status = 'pending', last_error = NULL, updated_at = now()
                 WHERE session_id = ? AND status = 'running'
                """,
                [(session_id,) for session_id in session_ids],
            )
    finally:
        con.close()
    return {
        "matched": len(session_ids),
        "reset": len(session_ids) if apply else 0,
        "apply": bool(apply),
        "stale_after_hours": stale_after_hours,
        "limit": limit,
        "session_ids": session_ids,
    }


def enqueue_span_embed(duckdb_path: Path, span_id: str) -> str:
    """Idempotent insert into span_embed_jobs for a span-derived embedding."""
    con = open_duckdb_connection(duckdb_path)
    try:
        existing = con.execute(
            "SELECT status FROM span_embed_jobs WHERE span_id=?",
            [span_id],
        ).fetchone()
        if existing:
            status = existing[0]
            if status == "done":
                return "already_done"
            if status in ("pending", "running"):
                return "already_queued"
            con.execute(
                "UPDATE span_embed_jobs SET status='pending', last_error=NULL, updated_at=now() WHERE span_id=?",
                [span_id],
            )
            return "requeued"
        con.execute(
            "INSERT INTO span_embed_jobs (span_id, status, attempts) VALUES (?, 'pending', 0)",
            [span_id],
        )
    finally:
        con.close()
    return "queued"


def _span_partition_dates(parquet_dir: Path, *, since_days: Optional[int]) -> list[str]:
    root = Path(parquet_dir) / "spans"
    if not root.exists():
        return []
    cutoff: Optional[date] = None
    if since_days is not None:
        cutoff = date.today() - timedelta(days=max(0, int(since_days)))
    dates: list[str] = []
    for child in root.iterdir():
        if not child.is_dir() or not child.name.startswith("date="):
            continue
        value = child.name.removeprefix("date=")
        if cutoff is not None:
            try:
                if date.fromisoformat(value) < cutoff:
                    continue
            except ValueError:
                continue
        dates.append(value)
    return sorted(dates)


def enqueue_missing_span_embeds(
    *,
    duckdb_path: Path,
    parquet_dir: Path,
    limit: int = 1000,
    apply: bool = False,
    since_days: Optional[int] = None,
) -> dict:
    """Queue missing span embedding jobs for existing spans.

    Dry-run by default. Existing span embeddings and existing jobs are skipped.
    When Hive span date partitions are present, reads are bounded to those dates
    through ``spans_for_date`` to avoid broad live parquet scans.
    """
    limit = max(1, int(limit))
    dates = _span_partition_dates(parquet_dir, since_days=since_days)
    if dates:
        source_sql = "\nUNION ALL\n".join(
            "SELECT * FROM spans_for_date(?)" for _ in dates
        )
        params: list = list(dates)
    else:
        source_sql = "SELECT * FROM spans"
        params = []

    sql = f"""
        WITH candidate_spans AS (
            {source_sql}
        )
        SELECT DISTINCT s.span_id
          FROM candidate_spans s
          LEFT JOIN span_embed_jobs j ON j.span_id = s.span_id
          LEFT JOIN span_embeddings e ON e.span_id = s.span_id
         WHERE s.span_id IS NOT NULL
           AND j.span_id IS NULL
           AND e.span_id IS NULL
         ORDER BY s.span_id
         LIMIT ?
    """
    con = open_duckdb_connection(duckdb_path, read_only=not apply, role="diagnostic")
    try:
        rows = con.execute(sql, [*params, limit]).fetchall()
        span_ids = [row[0] for row in rows]
        if apply and span_ids:
            con.executemany(
                """INSERT INTO span_embed_jobs (span_id, status, attempts)
                   VALUES (?, 'pending', 0)
                   ON CONFLICT (span_id) DO NOTHING""",
                [(span_id,) for span_id in span_ids],
            )
    finally:
        con.close()
    return {
        "candidate_count": len(span_ids),
        "enqueued": len(span_ids) if apply else 0,
        "apply": bool(apply),
    }


def reset_stale_span_embed_jobs(
    *,
    duckdb_path: Path,
    stale_after_hours: int = 24,
    limit: int = 1000,
    apply: bool = False,
) -> dict:
    """Safely reset stranded running span embed jobs back to pending.

    Dry-run by default. Only ``running`` jobs whose ``updated_at`` (or
    ``enqueued_at`` when ``updated_at`` is null) is older than the threshold are
    matched; fresh running jobs are left untouched.
    """
    stale_after_hours = max(1, int(stale_after_hours))
    limit = max(1, int(limit))
    con = open_duckdb_connection(duckdb_path, read_only=not apply, role="diagnostic")
    try:
        rows = con.execute(
            """
            SELECT span_id
              FROM span_embed_jobs
             WHERE status = 'running'
               AND COALESCE(updated_at, enqueued_at) < now() - (? * INTERVAL '1 hour')
             ORDER BY COALESCE(updated_at, enqueued_at) ASC NULLS FIRST, span_id
             LIMIT ?
            """,
            [stale_after_hours, limit],
        ).fetchall()
        span_ids = [str(row[0]) for row in rows]
        if apply and span_ids:
            con.executemany(
                """
                UPDATE span_embed_jobs
                   SET status = 'pending', last_error = NULL, updated_at = now()
                 WHERE span_id = ? AND status = 'running'
                """,
                [(span_id,) for span_id in span_ids],
            )
    finally:
        con.close()
    return {
        "matched": len(span_ids),
        "reset": len(span_ids) if apply else 0,
        "apply": bool(apply),
        "stale_after_hours": stale_after_hours,
        "limit": limit,
        "span_ids": span_ids,
    }
