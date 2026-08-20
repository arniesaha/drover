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
from dataclasses import dataclass
from pathlib import Path

import duckdb
from watchdog.events import FileSystemEvent, FileSystemEventHandler
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


#: One pass a day: the window is measured in days, so anything finer just
#: walks the tree for nothing.
_SWEEP_INTERVAL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class SweepResult:
    """What one retention pass reclaimed."""

    files: int = 0
    bytes: int = 0


def sweep_processed(incoming_dir: Path, *, retention_days: int) -> SweepResult:
    """Reclaim audit copies older than ``retention_days``.

    `processed_retention_days` has been in the config, in `DroverConfig` and in
    the documented example config since the beginning, and nothing enforced it.
    On the hub that came to 9.7GB of incoming, 8.6GB of which was 6,692 audit
    copies older than the seven days the operator had asked for. A policy
    nobody implements is worse than no policy, because it is written down and
    therefore trusted.

    Only `.processed` is swept. The watcher moves a file there after ingest
    *and* job enqueue have both succeeded, which makes it the one directory
    where deleting cannot lose anything; a file still sitting in the spool is
    waiting to be read and is never touched.

    Zero (or negative) declines rather than deleting everything. Zero is what
    an operator reaches for meaning "keep nothing", and equally what a
    malformed setting parses to -- and the failure mode of a typo should not
    be an erased audit trail.
    """

    if retention_days <= 0:
        return SweepResult()
    incoming = Path(incoming_dir)
    if not incoming.exists():
        return SweepResult()

    cutoff = time.time() - retention_days * 86400
    files = 0
    freed = 0
    for path in incoming.glob("*/.processed/*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            if stat.st_mtime >= cutoff:
                continue
            path.unlink()
        except OSError as exc:
            # One unreadable or already-gone file must not stop the pass:
            # the next one may be the large one.
            log.debug("could not reclaim %s: %s", path, exc)
            continue
        files += 1
        freed += stat.st_size
    if files:
        log.info(
            "reclaimed %d processed file(s), %.1f MB, older than %d day(s)",
            files,
            freed / 1_048_576,
            retention_days,
        )
    return SweepResult(files=files, bytes=freed)


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
        retention_days: int = 0,
    ):
        self._incoming = Path(incoming_dir)
        self._retention_days = int(retention_days)
        self._sweeper: threading.Thread | None = None
        self._stopping = threading.Event()
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
        self._start_sweeper()

    def _start_sweeper(self) -> None:
        """Sweep once at startup, then daily.

        At startup because a hub that has been down for a week comes back with
        a week of audit copies to reclaim, and daily because the window is
        measured in days: anything finer would just walk the tree for nothing.
        """

        if self._retention_days <= 0:
            return

        def loop() -> None:
            while not self._stopping.is_set():
                try:
                    sweep_processed(self._incoming, retention_days=self._retention_days)
                except Exception as exc:  # noqa: BLE001 - never kill the watcher
                    log.warning("processed sweep failed: %s", exc)
                self._stopping.wait(_SWEEP_INTERVAL_SECONDS)

        self._sweeper = threading.Thread(
            target=loop, name="drover-processed-sweep", daemon=True
        )
        self._sweeper.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            log.info("watcher stopped")
