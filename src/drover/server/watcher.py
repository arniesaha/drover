"""File-system watcher that ingests dropped AgentEvent JSONL files.

Layout watched:
  <incoming_dir>/<host>/<batch>.jsonl       <- act on this
  <incoming_dir>/<host>/<batch>.jsonl.tmp   <- ignore (in-flight)

After ingest, file is moved to:
  <incoming_dir>/<host>/.processed/<batch>.jsonl
"""

from __future__ import annotations
import json
import logging
import shutil
import threading
import time
from pathlib import Path

import duckdb
from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from drover.server.db import open_duckdb_connection
from drover.server.ingest import ingest_file
from drover.server.redis_shadow import ShadowPublisher
from drover.server.summarizer.jobs import (
    enqueue_summary_generation,
    publish_summary_generation,
    source_version_for_session,
)

log = logging.getLogger("drover.watcher")

_DUCKDB_LOCK_MARKERS = (
    "conflicting lock",
    "could not set lock",
    "database is locked",
    "duckdb write-lock",
    "write-lock contention",
)


def _is_duckdb_lock_contention(exc: BaseException) -> bool:
    """Return True for transient DuckDB lock-contention failures."""
    if not isinstance(exc, duckdb.Error):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _DUCKDB_LOCK_MARKERS)


def _session_ids_in_file(path: Path) -> set[str]:
    """Best-effort session_id extraction for idempotent summarize enqueue.

    The watcher may ingest rows successfully and then hit DuckDB lock contention
    while enqueuing summarize jobs. On retry, ingest dedupes the already-written
    events and no longer reports them as newly seen. Re-reading session ids from
    the source JSONL lets the enqueue phase recover idempotently before moving
    the file to .processed/.
    """
    session_ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = payload.get("session_id")
                if isinstance(sid, str) and sid:
                    session_ids.add(sid)
    except OSError:
        log.exception("failed to inspect session_ids in %s", path)
    return session_ids


class _Handler(FileSystemEventHandler):
    def __init__(
        self,
        parquet_dir: Path,
        duckdb_path: Path,
        *,
        max_lock_retries: int = 5,
        lock_retry_base_seconds: float = 1.0,
        shadow_publisher: ShadowPublisher | None = None,
        summarize_job_stream: object | None = None,
    ):
        self._parquet_dir = parquet_dir
        self._duckdb_path = duckdb_path
        self._lock = threading.Lock()  # serialize ingests; DuckDB single-writer
        self._max_lock_retries = max(0, int(max_lock_retries))
        self._lock_retry_base_seconds = max(0.0, float(lock_retry_base_seconds))
        self._shadow_publisher = shadow_publisher
        self._summarize_job_stream = summarize_job_stream

    def _maybe_ingest(self, path: Path) -> None:
        if not path.is_file():
            return
        if path.suffix != ".jsonl":  # ignore .tmp and anything else
            return
        if ".processed" in path.parts:  # ignore our own moves
            return

        with self._lock:
            attempts = self._max_lock_retries + 1
            for attempt in range(1, attempts + 1):
                try:
                    self._ingest_once(path)
                    return
                except Exception as exc:
                    if _is_duckdb_lock_contention(exc) and attempt < attempts:
                        delay = self._lock_retry_base_seconds * (2 ** (attempt - 1))
                        log.warning(
                            "DuckDB lock contention while ingesting %s; retrying attempt %d/%d in %.1fs",
                            path,
                            attempt + 1,
                            attempts,
                            delay,
                        )
                        if delay:
                            time.sleep(delay)
                        continue
                    if _is_duckdb_lock_contention(exc):
                        log.warning(
                            "DuckDB lock contention exhausted for %s after %d attempt(s); "
                            "leaving file in place. Run `drover-server runtime-audit` "
                            "to inspect pending incoming JSONL by source and restart the "
                            "writer if the backlog continues to age.",
                            path,
                            attempts,
                            exc_info=True,
                        )
                    else:
                        log.exception(
                            "ingest failed for %s; leaving file in place", path
                        )
                    return

    def _ingest_once(self, path: Path) -> None:
        stats = ingest_file(
            path,
            parquet_dir=self._parquet_dir,
            duckdb_path=self._duckdb_path,
            shadow_publisher=self._shadow_publisher,
        )
        log.info(
            "ingested %s read=%d inserted=%d dupes=%d errors=%d shadow=%d receipts=%d",
            path,
            stats.read,
            stats.inserted,
            stats.skipped_dupes,
            stats.errors,
            stats.shadow_published,
            stats.ledger_receipts,
        )
        # Enqueue summarize jobs idempotently. Include session IDs present in
        # the source file so a retry after post-ingest DuckDB lock contention
        # cannot move the file without recovering summarize jobs for the
        # already-inserted sessions.
        session_ids = set(stats.new_session_ids) | _session_ids_in_file(path)
        if session_ids:
            con = open_duckdb_connection(self._duckdb_path)
            try:
                for sid in session_ids:
                    source_version = source_version_for_session(con, str(sid))
                    enqueue_summary_generation(con, str(sid), source_version)
                    publish_summary_generation(
                        con,
                        str(sid),
                        source_version,
                        self._summarize_job_stream,
                    )
                log.info(
                    "enqueued %d summarize_job(s) for %s",
                    len(session_ids),
                    path,
                )
            finally:
                con.close()
        # Move to .processed/ for audit only after ingest + job enqueue succeed.
        processed = path.parent / ".processed"
        processed.mkdir(exist_ok=True)
        target = processed / path.name
        shutil.move(str(path), str(target))

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._maybe_ingest(Path(event.src_path))

    def on_moved(self, event) -> None:
        # Triggered by collector's atomic rename .jsonl.tmp → .jsonl
        if event.is_directory:
            return
        self._maybe_ingest(Path(event.dest_path))


def ingest_incoming_file_once(
    path: Path,
    *,
    parquet_dir: Path,
    duckdb_path: Path,
    summarize_job_stream: object | None = None,
    shadow_publisher: ShadowPublisher | None = None,
) -> None:
    """Ingest one incoming JSONL file through the same path as the watcher.

    This is a small operational escape hatch for stale incoming files: it keeps
    the watcher semantics (dedupe, summarize-job enqueue, optional Redis stream
    publish, and move to ``.processed``) without needing to start a long-lived
    watcher process.
    """
    handler = _Handler(
        parquet_dir,
        duckdb_path,
        shadow_publisher=shadow_publisher,
        summarize_job_stream=summarize_job_stream,
    )
    handler._maybe_ingest(path)


class IncomingWatcher:
    """Run a watchdog observer over <incoming_dir> and ingest JSONL files."""

    def __init__(
        self,
        *,
        incoming_dir: Path,
        parquet_dir: Path,
        duckdb_path: Path,
        shadow_publisher: ShadowPublisher | None = None,
        summarize_job_stream: object | None = None,
    ):
        self._incoming = Path(incoming_dir)
        self._parquet_dir = Path(parquet_dir)
        self._duckdb_path = Path(duckdb_path)
        self._observer: Observer | None = None
        self._handler = _Handler(
            self._parquet_dir,
            self._duckdb_path,
            shadow_publisher=shadow_publisher,
            summarize_job_stream=summarize_job_stream,
        )

    def start(self) -> None:
        self._incoming.mkdir(parents=True, exist_ok=True)
        # Pick up files already present at start time
        for jsonl in self._incoming.rglob("*.jsonl"):
            self._handler._maybe_ingest(jsonl)
        observer = Observer()
        observer.schedule(self._handler, str(self._incoming), recursive=True)
        observer.start()
        self._observer = observer
        log.info("watcher started on %s", self._incoming)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            log.info("watcher stopped")
