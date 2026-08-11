"""Generate fenced, incremental recaps for live harness sessions."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from drover.server.db import open_duckdb_connection
from drover.server.harness.recap_jobs import flush_live_recap_publications
from drover.server.harness.recap_prompt import (
    build_live_recap_prompt,
    normalize_live_recap,
)
from drover.server.jobs import Delivery
from drover.server.summarizer.backends import (
    BackendError,
    LLMBackend,
    SummarizerBackendConfig,
    select_backend,
)

log = logging.getLogger("drover.harness.recap_worker")

_CONTENT_EVENT_TYPES = (
    "user_input",
    "assistant_output",
    "tool_action",
    "tool_result",
)
_RETRY_BASE_SECONDS = 60
_RETRY_MAX_SECONDS = 3600
_RUNNING_LEASE_SECONDS = 300


@dataclass(frozen=True)
class _Claim:
    session_id: str
    source_seq: int
    attempts: int
    delivery: Delivery | None = None
    handled_only: bool = False


class LiveRecapWorker:
    """Drain durable recap jobs without allowing stale output to overwrite it."""

    def __init__(
        self,
        *,
        duckdb_path: Path,
        backend: LLMBackend | None = None,
        backend_config: SummarizerBackendConfig | None = None,
        job_stream: object | None = None,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self._backend = backend
        self._backend_config = backend_config
        self.job_stream = job_stream
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background polling loop once."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="drover-live-recap", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Request shutdown and wait briefly for an active generation."""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception:  # noqa: BLE001 - a later poll must still run.
                log.exception("live recap drain loop crashed")
            self._stop.wait(self.poll_interval_s)

    def drain_once(self) -> int:
        """Process one recap generation, returning one when a delivery was handled."""
        self._flush_publications()
        claim = self._claim_stream_job() if self.job_stream is not None else None
        if claim is None:
            # Redis coordinates immediate work, but DuckDB owns retry timing.
            # An acknowledged failed delivery is retried from the durable row.
            claim = self._claim_due_job()
        if claim is None:
            return 0
        if claim.handled_only:
            return 1

        try:
            backend = self._resolve_backend()
            prompt = build_live_recap_prompt(self._load_events(claim.session_id))
            result = backend.summarize(prompt)
            recap = normalize_live_recap(
                result.get("recap") if isinstance(result, dict) else None
            )
            if not recap:
                raise BackendError("live recap backend returned an empty recap")
        except BackendError as exc:
            self._finish_failure(claim, str(exc))
            return 1
        except (
            Exception
        ) as exc:  # noqa: BLE001 - persist configuration/data failures too.
            self._finish_failure(claim, str(exc))
            return 1

        completed = self._complete(claim, recap, backend.model)
        if claim.delivery is not None:
            # A completed delivery and a stale duplicate are both safe to ACK:
            # DuckDB is the generation authority and the row was fenced above.
            self.job_stream.ack(claim.delivery.id)  # type: ignore[union-attr]
        if not completed:
            log.info(
                "discarded stale live recap result for %s at sequence %s",
                claim.session_id,
                claim.source_seq,
            )
        return 1

    def _flush_publications(self) -> None:
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            flush_live_recap_publications(con, self.job_stream)
        finally:
            con.close()

    def _resolve_backend(self) -> LLMBackend:
        if self._backend is not None:
            return self._backend
        if self._backend_config is None:
            raise BackendError("no backend configured for live recaps")
        return select_backend(job_kind="live_recap", config=self._backend_config)

    def _claim_due_job(self) -> _Claim | None:
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            row = con.execute(
                """SELECT session_id, desired_source_seq
                   FROM live_recap_jobs
                   WHERE status='pending'
                      OR (status='retry_wait' AND next_run_at <= now())
                      OR (status='running'
                          AND updated_at <= now() - ? * INTERVAL '1 second')
                   ORDER BY enqueued_at ASC
                   LIMIT 1""",
                [_RUNNING_LEASE_SECONDS],
            ).fetchone()
            if row is None:
                return None
            session_id, source_seq = str(row[0]), int(row[1])
            claimed = con.execute(
                """UPDATE live_recap_jobs
                   SET status='running', attempts=attempts + 1,
                       updated_at=now(), next_run_at=NULL
                   WHERE session_id=? AND desired_source_seq=?
                     AND (status='pending'
                       OR (status='retry_wait' AND next_run_at <= now())
                       OR (status='running'
                           AND updated_at <= now() - ? * INTERVAL '1 second'))
                   RETURNING attempts""",
                [session_id, source_seq, _RUNNING_LEASE_SECONDS],
            ).fetchone()
            if claimed is None:
                return None
            return _Claim(session_id, source_seq, int(claimed[0]))
        finally:
            con.close()

    def _claim_stream_job(self) -> _Claim | None:
        deliveries = self.job_stream.read_group("live-recap", count=1)  # type: ignore[union-attr]
        if not deliveries:
            deliveries = self.job_stream.reclaim("live-recap", count=1)  # type: ignore[union-attr]
        if not deliveries:
            return None
        delivery = deliveries[0]
        session_id = delivery.fields.get("session_id")
        source_seq = delivery.fields.get("source_seq")
        try:
            source_seq = int(source_seq)
        except (TypeError, ValueError):
            self.job_stream.fail(delivery.id, "missing or invalid source_seq")  # type: ignore[union-attr]
            return None
        if not session_id:
            self.job_stream.fail(delivery.id, "missing session_id")  # type: ignore[union-attr]
            return None
        session_id = str(session_id)

        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            row = con.execute(
                """SELECT desired_source_seq, status, next_run_at
                   FROM live_recap_jobs WHERE session_id=?""",
                [session_id],
            ).fetchone()
            if row is None or int(row[0]) != source_seq:
                self.job_stream.ack(delivery.id)  # type: ignore[union-attr]
                return _Claim(session_id, source_seq, 0, handled_only=True)

            desired_seq, status, next_run_at = int(row[0]), str(row[1]), row[2]
            if status == "done":
                self.job_stream.ack(delivery.id)  # type: ignore[union-attr]
                return _Claim(session_id, source_seq, 0, handled_only=True)
            if status == "retry_wait" and (
                next_run_at is None
                or next_run_at > con.execute("SELECT now()").fetchone()[0]
            ):
                defer = getattr(self.job_stream, "defer", None)
                if defer is not None and next_run_at is not None:
                    defer(delivery.id, until_ms=int(next_run_at.timestamp() * 1000))
                return None
            claimed = con.execute(
                """UPDATE live_recap_jobs
                   SET status='running', attempts=attempts + 1,
                       updated_at=now(), next_run_at=NULL
                   WHERE session_id=? AND desired_source_seq=?
                     AND (status='pending'
                       OR (status='retry_wait' AND next_run_at <= now())
                       OR (status='running' AND ? > 1))
                   RETURNING attempts""",
                [session_id, desired_seq, delivery.delivery_count],
            ).fetchone()
            if claimed is None:
                return None
            return _Claim(session_id, desired_seq, int(claimed[0]), delivery)
        finally:
            con.close()

    def _load_events(self, session_id: str) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in _CONTENT_EVENT_TYPES)
        con = open_duckdb_connection(self.duckdb_path, read_only=True, role="worker")
        try:
            cur = con.execute(
                f"""SELECT seq, event_type, content_preview
                    FROM harness_events
                   WHERE session_id=? AND seq IS NOT NULL
                     AND event_type IN ({placeholders})
                     AND content_preview IS NOT NULL AND content_preview <> ''
                   ORDER BY seq DESC
                   LIMIT 30""",
                [session_id, *_CONTENT_EVENT_TYPES],
            )
            return [
                dict(zip((column[0] for column in cur.description), row))
                for row in reversed(cur.fetchall())
            ]
        finally:
            con.close()

    def _complete(self, claim: _Claim, recap: str, model: str) -> bool:
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            con.execute("BEGIN TRANSACTION")
            persisted = con.execute(
                """INSERT OR REPLACE INTO live_session_recaps
                   (session_id, recap_text, source_seq, generator_model, generated_at)
                   SELECT ?, ?, ?, ?, now()
                   WHERE EXISTS (
                     SELECT 1 FROM live_recap_jobs
                      WHERE session_id=? AND desired_source_seq=? AND status='running'
                   )
                   RETURNING session_id""",
                [
                    claim.session_id,
                    recap,
                    claim.source_seq,
                    model,
                    claim.session_id,
                    claim.source_seq,
                ],
            ).fetchone()
            if persisted is None:
                con.execute("ROLLBACK")
                return False
            finalized = con.execute(
                """UPDATE live_recap_jobs
                   SET status='done', last_error=NULL, next_run_at=NULL, updated_at=now()
                   WHERE session_id=? AND desired_source_seq=? AND status='running'
                   RETURNING session_id""",
                [claim.session_id, claim.source_seq],
            ).fetchone()
            if finalized is None:
                con.execute("ROLLBACK")
                return False
            con.execute("COMMIT")
            return True
        except Exception:
            try:
                con.execute("ROLLBACK")
            except duckdb.Error:
                pass
            raise
        finally:
            con.close()

    def _finish_failure(self, claim: _Claim, error: str) -> None:
        delay_s = min(
            _RETRY_BASE_SECONDS * (2 ** max(claim.attempts - 1, 0)),
            _RETRY_MAX_SECONDS,
        )
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            con.execute(
                """UPDATE live_recap_jobs
                   SET status='retry_wait', last_error=?,
                       next_run_at=now() + ? * INTERVAL '1 second', updated_at=now()
                   WHERE session_id=? AND desired_source_seq=? AND status='running'
                   RETURNING session_id""",
                [error[:1000], delay_s, claim.session_id, claim.source_seq],
            ).fetchone()
        finally:
            con.close()
        if claim.delivery is not None:
            # The durable retry row is authoritative. ACK this delivery so
            # exponential waits cannot consume the stream redelivery budget.
            self.job_stream.ack(claim.delivery.id)  # type: ignore[union-attr]
