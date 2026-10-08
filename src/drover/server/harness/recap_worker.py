"""Generate fenced, incremental recaps for live harness sessions.

Recap work is ``recap_session`` jobs in the PostgreSQL job ledger (#480). The
worker claims one due job, generates a recap from the session's newest
content events, and in one transaction completes the job and advances the
live phase of ``session_memory``. The ledger's lease token is the fence: a
job superseded by a newer completion (or reclaimed after its lease expired)
cannot complete, so its stale recap rolls back with it.

On a DuckDB control plane derived memory is unavailable and the drain is a
no-op.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

from drover.server.db import control_plane_connection
from drover.server.harness.recap_prompt import (
    build_live_recap_prompt,
    normalize_live_recap,
)
from drover.server.harness.registry import HarnessRegistry
from drover.server.ledger import (
    RECAP_SESSION,
    ClaimedJob,
    JobLedger,
    memory_store_available,
    transaction,
)
from drover.server.memory_store import MemoryRepository
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
#: How long a job waits when no backend is configured. Releasing does not
#: spend an attempt: the job is fine, this worker just cannot run it yet.
_NO_BACKEND_RELEASE_SECONDS = 300


class _NoBackend(Exception):
    """No backend is configured for live recaps."""


class _StaleLease(Exception):
    """Roll the completion transaction back: the lease is no longer ours."""


class LiveRecapWorker:
    """Drain ``recap_session`` jobs without letting stale output overwrite newer."""

    def __init__(
        self,
        *,
        duckdb_path: Path,
        backend: LLMBackend | None = None,
        backend_config: SummarizerBackendConfig | None = None,
        poll_interval_s: float = 1.0,
        idle_poll_interval_s: float = 5.0,
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self._backend = backend
        self._backend_config = backend_config
        if poll_interval_s <= 0 or idle_poll_interval_s < poll_interval_s:
            raise ValueError("poll intervals must be positive and idle >= active")
        self.poll_interval_s = poll_interval_s
        self.idle_poll_interval_s = idle_poll_interval_s
        self._next_reconcile_at = 0.0
        self.worker_id = f"live-recap@{socket.gethostname()}:{os.getpid()}"
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
        interval = self.poll_interval_s
        while not self._stop.is_set():
            handled = 0
            try:
                handled = self.drain_once()
            except Exception:  # noqa: BLE001 - a later poll must still run.
                log.exception("live recap drain loop crashed")
            interval = (
                self.poll_interval_s
                if handled
                else min(interval * 2, self.idle_poll_interval_s)
            )
            self._stop.wait(interval)

    def drain_once(self) -> int:
        """Process one recap job, returning one when a job was handled."""
        # Completions that arrived before their session row are re-enqueued
        # here; on a DuckDB control plane that enqueue is itself a no-op, but
        # the marker still clears.
        now = time.monotonic()
        if now >= self._next_reconcile_at:
            HarnessRegistry(self.duckdb_path).reconcile_orphan_completions()
            self._next_reconcile_at = now + self.idle_poll_interval_s
        if not memory_store_available(self.duckdb_path):
            return 0
        ledger = JobLedger(self.duckdb_path)
        if not ledger.has_due(RECAP_SESSION):
            return 0
        claimed = ledger.claim(RECAP_SESSION, worker_id=self.worker_id, limit=1)
        if not claimed:
            return 0
        job = claimed[0]
        session_id = job.subject_key
        source_seq = self._source_seq(job)

        try:
            backend = self._resolve_backend()
            prompt = build_live_recap_prompt(self._load_events(session_id))
            result = backend.summarize(prompt)
            recap = normalize_live_recap(
                result.get("recap") if isinstance(result, dict) else None
            )
            if not recap:
                raise BackendError("live recap backend returned an empty recap")
        except _NoBackend as exc:
            ledger.release(
                job, delay_seconds=_NO_BACKEND_RELEASE_SECONDS, reason=str(exc)
            )
            return 1
        except BackendError as exc:
            self._fail(ledger, job, str(exc), "backend_error")
            return 1
        except Exception as exc:  # noqa: BLE001 - persist data failures too.
            self._fail(ledger, job, str(exc), type(exc).__name__)
            return 1

        if not self._complete(ledger, job, recap, backend.model, source_seq):
            log.info(
                "discarded stale live recap result for %s at sequence %s",
                session_id,
                source_seq,
            )
        return 1

    @staticmethod
    def _source_seq(job: ClaimedJob) -> int:
        seq = job.payload.get("source_seq") if job.payload else None
        return int(seq if seq is not None else job.source_version)

    def _resolve_backend(self) -> LLMBackend:
        if self._backend is not None:
            return self._backend
        if self._backend_config is None:
            raise _NoBackend("no backend configured for live recaps")
        return select_backend(job_kind="live_recap", config=self._backend_config)

    @staticmethod
    def _fail(ledger: JobLedger, job: ClaimedJob, error: str, category: str) -> None:
        # Retryable, bounded by the RECAP_SESSION policy: after max_attempts
        # the job dead-letters instead of retrying forever. A previously
        # generated recap is left in place either way.
        outcome = ledger.fail(job, error, retryable=True, category=category)
        log.warning(
            "live recap for %s at %s failed (%s): %s",
            job.subject_key,
            job.source_version,
            outcome,
            error,
        )

    def _load_events(self, session_id: str) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in _CONTENT_EVENT_TYPES)
        with control_plane_connection(self.duckdb_path) as con:
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

    def _complete(
        self,
        ledger: JobLedger,
        job: ClaimedJob,
        recap: str,
        model: str | None,
        source_seq: int,
    ) -> bool:
        """Complete the job and advance the live phase, or neither."""
        with ledger.connection() as con:
            try:
                with transaction(con):
                    if not ledger.complete(job, con=con):
                        raise _StaleLease()
                    MemoryRepository.put_recap(
                        con, job.subject_key, recap, source_seq, model
                    )
            except _StaleLease:
                return False
        return True
