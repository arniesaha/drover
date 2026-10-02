"""EmbedWorker — drains ``embed_session`` ledger jobs into pgvector (#480).

One job per session: embed the final summary (``session_memory.summary_md``)
and upsert it into ``session_embeddings`` in the PostgreSQL control store.
The job ledger (:mod:`drover.server.ledger`) is the only queue. There is no
DuckDB queue, no DuckDB vector table, no Redis delivery stream and no ledger
shadow left on this path, and span embeddings are gone from the core path
entirely (#473).

Outcomes per claimed job:

* summary missing, or at a different ``source_version`` than the job -- the
  job is for a stale generation: ``superseded`` (the summarizer enqueues the
  right one in the same commit as the newer summary);
* no embedder configured, embedder model differs from the configured model,
  or the embedder is not ready -- ``release`` without spending an attempt.
  This is #471's silent-disable case: the job is fine, the worker cannot run
  it, and readiness reports the stuck queue;
* ``embed_batch`` raises :class:`BackendError` -- a retryable failure;
* the vector does not fit the configured embedding space
  (:class:`EmbeddingMismatch`) -- ``quarantined`` with the reason. Each row is
  persisted in its own transaction, so one bad row never blocks the batch
  (#471 head-of-line poison);
* pgvector is not installed (:class:`VectorStoreUnavailable`) -- released
  with a long delay and one ERROR per drain naming pgvector.

A worker that dies mid-job leaves an expired lease; the next claim reclaims
it, so there is no "reset stale running jobs" operation any more.

The configured API-first embedding backend is used; the local Ollama/GPU path
remains a fallback for offline/no-API operation.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from drover.server.embeddings.client import (
    DEFAULT_EMBED_MODEL,
    EmbeddingBackendConfig,
)
from drover.server.ledger import (
    EMBED_SESSION,
    ClaimedJob,
    JobLedger,
    memory_store_available,
    transaction,
)
from drover.server.memory_store import (
    EmbeddingMismatch,
    EmbeddingStore,
    MemoryRepository,
    SessionSummary,
    VectorStoreUnavailable,
)
from drover.server.postgres_schema import EMBEDDING_DIM
from drover.server.summarizer.backends import SummarizerBackendConfig
from drover.server.summarizer.backends.types import BackendError

log = logging.getLogger("drover.embeddings.worker")

# Release delays (seconds) for conditions outside the job. None of them spend
# an attempt; they only keep a worker from hot-looping on a queue it cannot
# serve.
NO_EMBEDDER_RELEASE_S = 300.0
NOT_READY_RELEASE_S = 120.0
MODEL_MISMATCH_RELEASE_S = 300.0
VECTOR_UNAVAILABLE_RELEASE_S = 300.0


class _LeaseLost(Exception):
    """``complete`` refused: the lease was reclaimed or superseded. Roll back."""


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
        worker_id: str = "embeddings",
        spans_enabled: bool = False,
    ) -> None:
        # The control-store registration path: JobLedger, MemoryRepository and
        # EmbeddingStore all resolve the PostgreSQL store from it.
        self.duckdb_path = Path(duckdb_path)
        self._embedder = embedder
        self._backend_config = backend_config
        self._embedding_config = embedding_config
        self._resolved_embedder: Optional[object] = None
        self.embed_model = embed_model
        self.batch_size = max(1, int(batch_size))
        self.poll_interval_s = poll_interval_s
        self.worker_id = worker_id
        self._unavailable_logged = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def configured_model(self) -> str:
        """The embedding space vectors are written into.

        With an :class:`EmbeddingBackendConfig` this is the model the selected
        backend uses (the API model when an API embedder is configured, the
        local Ollama model otherwise); without one it is ``embed_model``, which
        is also what the GPU-rig fallback embeds with.
        """
        cfg = self._embedding_config
        if cfg is not None:
            return cfg.api_model if cfg.has_api_embedder else cfg.local_model
        return self.embed_model

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

    def _ledger(self) -> Optional[JobLedger]:
        """The PG job ledger, or None (logged once) when memory is unavailable."""
        if not memory_store_available(self.duckdb_path):
            if not self._unavailable_logged:
                log.warning(
                    "embed worker: derived memory requires the PostgreSQL control "
                    "store; session embeddings are disabled"
                )
                self._unavailable_logged = True
            return None
        return JobLedger(self.duckdb_path)

    def start(self) -> None:
        if self._thread is not None:
            return
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
        """Claim and process up to ``max_jobs`` embed jobs.

        Returns the number of jobs that reached an outcome this drain
        (succeeded, superseded, failed or quarantined); released jobs do not
        count. An empty queue never resolves or warms the embedder.
        """
        if max_jobs is None:
            max_jobs = self.batch_size
        ledger = self._ledger()
        if ledger is None or not ledger.has_due(EMBED_SESSION):
            return 0
        jobs = ledger.claim(EMBED_SESSION, worker_id=self.worker_id, limit=max_jobs)
        if not jobs:
            return 0

        summaries = MemoryRepository(self.duckdb_path).summaries(
            job.subject_key for job in jobs
        )
        processed = 0
        runnable: list[tuple[ClaimedJob, SessionSummary]] = []
        for job in jobs:
            summary = summaries.get(job.subject_key)
            if summary is None:
                ledger.supersede(job, "session summary missing; nothing to embed")
                processed += 1
            elif (summary.source_version or "") != (job.source_version or ""):
                ledger.supersede(
                    job,
                    f"summary is at source version {summary.source_version or '(none)'}; "
                    f"job was for {job.source_version or '(none)'}",
                )
                processed += 1
            else:
                runnable.append((job, summary))
        if not runnable:
            return processed

        model = self.configured_model
        try:
            embedder = self._resolve_embedder()
        except BackendError as e:
            log.warning("embed worker: embedder selection failed: %s", e)
            embedder = None
        if embedder is None:
            log.debug("embed worker: no embedder configured; releasing jobs")
            self._release_all(
                ledger,
                runnable,
                NO_EMBEDDER_RELEASE_S,
                "no embedder configured (no API, Mac-local Ollama or GPU rig)",
            )
            return processed
        embedder_model = getattr(embedder, "model", None)
        if embedder_model != model:
            reason = (
                f"embedder model {embedder_model!r} does not match the configured "
                f"embedding model {model!r}"
            )
            log.error("embed worker: %s; releasing jobs", reason)
            self._release_all(ledger, runnable, MODEL_MISMATCH_RELEASE_S, reason)
            return processed
        try:
            embedder.ensure_ready()
        except Exception as e:  # noqa: BLE001 - readiness can fail below BackendError.
            log.warning("embed ensure_ready failed: %s", e)
            self._release_all(
                ledger, runnable, NOT_READY_RELEASE_S, f"embedder not ready: {e}"
            )
            return processed

        texts = [summary.summary_md or "" for _, summary in runnable]
        try:
            vectors = embedder.embed_batch(texts)
            if not isinstance(vectors, (list, tuple)):
                raise BackendError("embedder returned an invalid vector batch")
            if len(vectors) != len(runnable):
                raise BackendError(
                    f"embedder returned {len(vectors)} vectors for {len(runnable)} texts"
                )
        except Exception as e:  # Backend shape/transport faults spend bounded retries.
            log.warning("session embed batch failed: %s", e)
            for job, _ in runnable:
                ledger.fail(job, str(e), retryable=True, category="embed_backend")
            return processed + len(runnable)

        wrong_dims = {
            len(v)
            for v in vectors
            if isinstance(v, (list, tuple)) and len(v) != EMBEDDING_DIM
        }
        if (
            wrong_dims
            and len(wrong_dims) == 1
            and all(
                isinstance(v, (list, tuple)) and len(v) != EMBEDDING_DIM
                for v in vectors
            )
        ):
            # Every vector is the wrong size: that is the embedder's space, not
            # these rows (e.g. a 1536-d API model against vector(768)). Burning
            # every job's budget into quarantine would only hide the config.
            reason = (
                f"embedder {embedder_model!r} produces {wrong_dims.pop()}-d vectors; "
                f"session_embeddings is vector({EMBEDDING_DIM})"
            )
            log.error("embed worker: %s; releasing jobs", reason)
            self._release_all(ledger, runnable, MODEL_MISMATCH_RELEASE_S, reason)
            return processed

        store = EmbeddingStore(self.duckdb_path, model=model)
        vector_error_logged = False
        for (job, _), vector in zip(runnable, vectors):
            try:
                self._persist(ledger, store, job, vector, embedder_model)
            except EmbeddingMismatch as e:
                log.warning("embedding for %s quarantined: %s", job.subject_key, e)
                ledger.fail(job, str(e), retryable=False, category="embedding_mismatch")
            except VectorStoreUnavailable as e:
                if not vector_error_logged:
                    log.error(
                        "embed worker: pgvector unavailable, releasing jobs: %s", e
                    )
                    vector_error_logged = True
                ledger.release(
                    job,
                    delay_seconds=VECTOR_UNAVAILABLE_RELEASE_S,
                    reason=f"pgvector unavailable: {e}",
                )
                continue
            except _LeaseLost:
                log.info(
                    "embed job for %s lost its lease before commit; discarded",
                    job.subject_key,
                )
            except Exception as e:  # noqa: BLE001 - one row never blocks the batch.
                log.warning("persist embedding for %s failed: %s", job.subject_key, e)
                ledger.fail(job, str(e), retryable=True, category="embed_persist")
            processed += 1
        return processed

    @staticmethod
    def _release_all(
        ledger: JobLedger,
        runnable: list[tuple[ClaimedJob, SessionSummary]],
        delay_seconds: float,
        reason: str,
    ) -> None:
        for job, _ in runnable:
            ledger.release(job, delay_seconds=delay_seconds, reason=reason)

    @staticmethod
    def _persist(
        ledger: JobLedger,
        store: EmbeddingStore,
        job: ClaimedJob,
        vector: list[float],
        model: str,
    ) -> None:
        """Vector upsert and job completion commit together, or not at all."""
        store._validate(vector, model)
        with ledger.connection() as con:
            with transaction(con):
                # Lock the job before the vector row: the summarizer also
                # locks its job before publishing downstream intent.
                if not ledger.complete(
                    job, con=con, metrics={"model": model, "dim": len(vector)}
                ):
                    raise _LeaseLost()
                store.put(
                    con,
                    job.subject_key,
                    vector,
                    model=model,
                    source_version=job.source_version,
                )
