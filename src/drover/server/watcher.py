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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from drover.server.db import control_plane_connection, open_duckdb_connection
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


#: Receipt kinds a sweep may reclaim. An allow-list, not a deny-list, so a new
#: `source_kind` -- the Loop Engine's work units, say -- is retained until
#: somebody argues otherwise here.
#:
#: Both of these are written by `ledger_shadow` and read by nothing. Ingest
#: dedupes against the Parquet partition (`_existing_dedup_keys`) *before* the
#: receipt is written, so removing one cannot make its source unit
#: reprocessable. `advisory_target_snapshot` is absent deliberately: the
#: advisory path reads it by `source_kind`, and it is 0.9% of the table anyway.
SWEEPABLE_RECEIPT_KINDS = ("agent_event", "otlp_span")


@dataclass(frozen=True)
class ReceiptSweepResult:
    """What one receipt retention pass removed."""

    receipts: int = 0


def sweep_receipts(duckdb_path: Path, *, retention_days: int) -> ReceiptSweepResult:
    """Reclaim shadow receipts older than ``retention_days``.

    `pipeline_receipts` had no delete path of any kind: 1.87M rows over two
    months, 60k to 140k a day, and no downward pressure (#255). Store size is
    an input to every analytical query's cost, so this grew the number #247 is
    measured against, quietly, every day.

    Two safety properties, rather than a size target:

    * only kinds in `SWEEPABLE_RECEIPT_KINDS`, which nothing reads; and
    * never a receipt some job still names through `caused_by_receipt_id`.
      The advisory worker joins jobs to receipts through that column, and a
      deleted receipt would not fail loudly -- the join would simply return
      nothing.

    Zero (or negative) declines rather than deleting everything, matching
    `sweep_processed`: zero is what an operator reaches for meaning "keep
    nothing", and equally what a malformed setting parses to.

    **This does not shrink the file.** DuckDB does not return space on DELETE
    without a rewrite, so the row count falls and `drover.duckdb` stays the
    size it was until something compacts it. Said plainly here because a
    retention pass that looks like a no-op invites someone to raise the
    setting until it appears to work.
    """

    if retention_days <= 0:
        return ReceiptSweepResult()

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    placeholders = ", ".join("?" for _ in SWEEPABLE_RECEIPT_KINDS)
    con = duckdb.connect(str(duckdb_path))
    try:
        removed = con.execute(
            f"""
            DELETE FROM pipeline_receipts
             WHERE source_kind IN ({placeholders})
               AND first_seen_at < ?
               AND NOT EXISTS (
                   SELECT 1 FROM pipeline_jobs j
                    WHERE j.caused_by_receipt_id = pipeline_receipts.receipt_id
               )
            """,
            [*SWEEPABLE_RECEIPT_KINDS, cutoff],
        ).fetchone()
    finally:
        con.close()

    count = int(removed[0]) if removed and removed[0] is not None else 0
    if count:
        log.info(
            "reclaimed %d shadow receipt(s) older than %d day(s); the file does "
            "not shrink until it is compacted",
            count,
            retention_days,
        )
    return ReceiptSweepResult(receipts=count)


@dataclass(frozen=True)
class OccurrenceSweepResult:
    """What one advisory-occurrence retention pass removed."""

    occurrences: int = 0


def sweep_advisory_occurrences(
    duckdb_path: Path, *, retention_days: int
) -> OccurrenceSweepResult:
    """Reclaim ``advisory_occurrences`` rows older than ``retention_days`` (#302).

    Findings and occurrences moved into the control-plane store, which has no
    retention pass of its own; every occurrence a run has ever produced piles
    up forever. This mirrors `sweep_receipts`'s shape but opens the
    control-plane store -- `control_plane_connection` resolves the analytical
    path handed in here to the registry file itself -- rather than a bare
    `duckdb.connect`.

    One safety property, not a size target: never a finding's newest failing
    occurrence, whatever its age. `AdvisoryRepository._next_observed_state`
    (repository.py's dismissal-regression check) and its material-change
    detection both read the latest failing row per finding to decide whether
    a dismissed finding should reopen; sweeping that row out from under them
    would make every dismissed finding look untouched forever. A row is
    deleted only when it is both older than the cutoff *and* superseded by a
    later (or same-instant, larger `occurrence_id`) failing row of the same
    finding -- so the newest failing row always survives, and a finding with
    only passing rows keeps them unless a later failing row exists to
    supersede them, which is acceptable: nothing reads a passing-only
    finding's occurrence history for the dismissal-regression check.

    Zero (or negative) declines rather than deleting everything, matching
    `sweep_receipts`: zero is what an operator reaches for meaning "keep
    nothing", and equally what a malformed setting parses to.

    **This does not shrink the file.** Same caveat as `sweep_receipts`: the
    row count falls, the file does not, until something compacts it.
    """

    if retention_days <= 0:
        return OccurrenceSweepResult()

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    with control_plane_connection(duckdb_path) as con:
        removed = con.execute(
            """
            DELETE FROM advisory_occurrences o
            WHERE o.recorded_at < ?
              AND EXISTS (
                  SELECT 1 FROM advisory_occurrences newer
                   WHERE newer.finding_id = o.finding_id AND newer.outcome = 'failing'
                     AND (newer.recorded_at > o.recorded_at
                          OR (newer.recorded_at = o.recorded_at
                              AND newer.occurrence_id > o.occurrence_id))
              )
            """,
            [cutoff],
        ).fetchone()

    count = int(removed[0]) if removed and removed[0] is not None else 0
    if count:
        log.info(
            "reclaimed %d advisory occurrence(s) older than %d day(s); the file "
            "does not shrink until it is compacted",
            count,
            retention_days,
        )
    return OccurrenceSweepResult(occurrences=count)


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
        receipt_retention_days: int = 0,
        advisory_occurrence_retention_days: int = 0,
    ):
        self._incoming = Path(incoming_dir)
        self._retention_days = int(retention_days)
        self._receipt_retention_days = int(receipt_retention_days)
        self._advisory_occurrence_retention_days = int(
            advisory_occurrence_retention_days
        )
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

        if (
            self._retention_days <= 0
            and self._receipt_retention_days <= 0
            and self._advisory_occurrence_retention_days <= 0
        ):
            return

        def loop() -> None:
            while not self._stopping.is_set():
                try:
                    sweep_processed(self._incoming, retention_days=self._retention_days)
                except Exception as exc:  # noqa: BLE001 - never kill the watcher
                    log.warning("processed sweep failed: %s", exc)
                try:
                    sweep_receipts(
                        self._duckdb_path,
                        retention_days=self._receipt_retention_days,
                    )
                except Exception as exc:  # noqa: BLE001 - never kill the watcher
                    log.warning("receipt sweep failed: %s", exc)
                try:
                    sweep_advisory_occurrences(
                        self._duckdb_path,
                        retention_days=self._advisory_occurrence_retention_days,
                    )
                except Exception as exc:  # noqa: BLE001 - never kill the watcher
                    log.warning("advisory occurrence sweep failed: %s", exc)
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
