"""Analytics-role lifecycle for the durable PostgreSQL harness outbox.

The exporter deliberately has one small responsibility: turn committed control
events into the immutable relation that analytical jobs may read.  It does not
compact those files or substitute the collector's ``agent_events`` relation.
Every restart first replays the SQL manifest, so a crash between publish and
acknowledgement cannot look like an empty backlog.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from drover.server.control_outbox import (
    LocalVerifiedArchiveResolver,
    acknowledge_outbox_batch,
    claim_outbox_batch,
    outbox_status,
    prune_verified_payloads,
    publish_outbox_batch,
    published_batches,
    register_published_harness_events_relation,
)
from drover.server.control_store import is_postgres_control_store
from drover.server.db import control_plane_connection, open_duckdb_connection

log = logging.getLogger("drover.control_exporter")


class ControlOutboxExporter:
    """Bounded background exporter owned by ``all`` and ``analytics`` roles."""

    def __init__(
        self,
        *,
        control_path: Path,
        analytical_path: Path,
        parquet_dir: Path,
        batch_size: int = 100,
        flush_age_seconds: float = 5.0,
        poll_seconds: float = 1.0,
        lease_seconds: int = 60,
        retention_limit: int = 100,
        owner: str | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("outbox batch_size must be positive")
        if (
            flush_age_seconds <= 0
            or poll_seconds <= 0
            or lease_seconds < 1
            or retention_limit < 1
        ):
            raise ValueError("outbox exporter timing limits must be positive")
        self.control_path = Path(control_path)
        self.analytical_path = Path(analytical_path)
        self.parquet_dir = Path(parquet_dir)
        self.batch_size = batch_size
        self.flush_age_seconds = float(flush_age_seconds)
        self.poll_seconds = float(poll_seconds)
        self.lease_seconds = int(lease_seconds)
        self.retention_limit = int(retention_limit)
        self.owner = owner or f"analytics-exporter-{uuid4().hex}"
        self._thread: threading.Thread | None = None
        self._shutdown: threading.Event | None = None
        self._lock = threading.Lock()
        self._last_error: str | None = None
        self._last_result: dict[str, Any] = self._empty_result()

    @staticmethod
    def _empty_result() -> dict[str, Any]:
        return {
            "enabled": False,
            "published": 0,
            "acknowledged": 0,
            "pending": 0,
            "claimed": 0,
            "published_unacknowledged": 0,
            "oldest_outstanding_at": None,
            "retention_pruned": 0,
            "retention_verification_failed": 0,
        }

    def start(self, *, shutdown_event: threading.Event) -> None:
        """Run one bounded pass per poll interval until the owning role stops."""
        if self._thread is not None:
            return
        self._shutdown = shutdown_event
        self._thread = threading.Thread(
            target=self._run, name="drover-control-outbox", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._shutdown is not None:
            self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_seconds * 3))

    def health(self) -> dict[str, Any]:
        """Return lag and the latest bounded failure for worker readiness."""
        with self._lock:
            return {**self._last_result, "last_error": self._last_error}

    def _run(self) -> None:
        assert self._shutdown is not None
        while not self._shutdown.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - retry durable work later
                log.exception("control outbox export pass failed")
                with self._lock:
                    self._last_error = type(exc).__name__
            self._shutdown.wait(self.poll_seconds)

    def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Publish at most one flush-sized batch and always resume SQL receipts."""
        if not is_postgres_control_store(self.control_path):
            result = self._empty_result()
            with self._lock:
                self._last_result = result
                self._last_error = None
            return result

        stamp = now or datetime.now(timezone.utc)
        with control_plane_connection(self.control_path) as control:
            before = outbox_status(control)

        # A published row is already a SQL-visible immutable receipt.  Rebuild
        # the analytical view before acknowledgement, including after a crash
        # between those operations.  No glob or generic compaction is involved.
        acknowledged = self._rebuild_relation_and_acknowledge()
        published = 0

        should_claim = self._should_claim(before, stamp)
        if should_claim:
            with control_plane_connection(self.control_path) as control:
                claim = claim_outbox_batch(
                    control,
                    owner=self.owner,
                    limit=self.batch_size,
                    lease_seconds=self.lease_seconds,
                    now=stamp,
                )
                if claim is not None:
                    receipt = publish_outbox_batch(
                        control,
                        claim,
                        parquet_dir=self.parquet_dir,
                        now=stamp,
                    )
                    published = receipt.member_count
            if published:
                acknowledged += self._rebuild_relation_and_acknowledge()

        with control_plane_connection(self.control_path) as control:
            current = outbox_status(control)
        retention = self._prune_verified_payloads()
        result = {
            **current,
            "published": published,
            "acknowledged": acknowledged,
            "retention_pruned": retention["pruned"],
            "retention_verification_failed": retention["verification_failed"],
        }
        with self._lock:
            self._last_result = result
            self._last_error = None
        return result

    def _should_claim(self, status: dict[str, Any], now: datetime) -> bool:
        # A claimed row needs a recovery attempt even when pending is zero.
        # ``claim_outbox_batch`` itself refuses an unexpired lease, so this is
        # safe during a normal overlapping poll and essential after restart.
        if int(status.get("claimed") or 0) > 0:
            return True
        pending = int(status.get("pending") or 0)
        if pending >= self.batch_size:
            return True
        oldest = status.get("oldest_pending_at")
        if pending and isinstance(oldest, datetime):
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            return (now - oldest).total_seconds() >= self.flush_age_seconds
        return False

    def _rebuild_relation_and_acknowledge(self) -> int:
        with control_plane_connection(self.control_path) as control:
            rows = control.execute(
                "SELECT batch_id FROM control_outbox_batches "
                "WHERE state = 'published' ORDER BY published_at, batch_id"
            ).fetchall()
            if not rows:
                return 0
            analytics = open_duckdb_connection(self.analytical_path)
            try:
                register_published_harness_events_relation(analytics, control)
            finally:
                analytics.close()
            acknowledged = 0
            for (batch_id,) in rows:
                if acknowledge_outbox_batch(control, str(batch_id)):
                    acknowledged += 1
            return acknowledged

    def _prune_verified_payloads(self) -> dict[str, int]:
        """Run retention through its detached control-read/verify/revalidate API."""

        def manifest_reader() -> set[str]:
            with control_plane_connection(self.control_path) as control:
                return {batch.batch_id for batch in published_batches(control)}

        return prune_verified_payloads(
            self.control_path,
            resolver=LocalVerifiedArchiveResolver(
                self.parquet_dir, manifest_reader=manifest_reader
            ),
            limit=self.retention_limit,
        )
