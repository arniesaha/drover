"""Backend-selected legacy sink for the durable control event outbox.

Committed collector and harness events become legacy parquet and the immutable
raw archive through this owner. Publication, projection and acknowledgement
are retry-safe; restart resumes receipts before derived jobs become eligible.
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
    OutboxClaim,
    _claim_rows,
    acknowledge_outbox_batch,
    claim_outbox_batch,
    export_projection,
    outbox_status,
    prune_acknowledged_outbox,
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
        acknowledgement_retention_days: float = 14,
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
        if acknowledgement_retention_days < 14:
            raise ValueError(
                "outbox acknowledgement retention must be at least 14 days"
            )
        self.acknowledgement_retention_days = acknowledgement_retention_days
        self._prune_cursor = ""
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
        self._relation_initialized = False
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
        from contextlib import ExitStack

        from drover.server.lake.writer_gate import legacy_derived_write

        # The public constructor permits separate control/analytical paths.
        # Fence both write namespaces in stable order, retaining reentrancy for
        # refresh_memory_projection's nested control-path gate.
        paths = sorted({self.control_path.resolve(), self.analytical_path.resolve()})
        with ExitStack() as fences:
            for path in paths:
                if not fences.enter_context(legacy_derived_write(path)):
                    return self._empty_result()
            return self._run_once_legacy(now=now)

    def _run_once_legacy(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Publish at most one flush-sized batch and always resume SQL receipts."""
        stamp = now or datetime.now(timezone.utc)
        with control_plane_connection(self.control_path) as control:
            before = outbox_status(control)

        # A published row is already a SQL-visible immutable receipt.  Rebuild
        # the analytical view before acknowledgement, including after a crash
        # between those operations.  No glob or generic compaction is involved.
        projected = []
        with control_plane_connection(self.control_path) as control:
            recovery = control.execute(
                "SELECT batch_id FROM control_outbox_batches WHERE state='published' ORDER BY published_at,batch_id LIMIT 1"
            ).fetchone()
            if recovery:
                from drover.server.legacy_outbox import write_legacy_events

                raw = _claim_rows(
                    control, OutboxClaim(str(recovery[0]), (), self.owner, stamp)
                )
                raw = self._projectable_rows(control, raw)
                projected = export_projection(control, raw)
                write_legacy_events(control, raw, self.parquet_dir, str(recovery[0]))
        acknowledged = self._rebuild_relation_and_acknowledge(acknowledge=False)
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
                    raw = self._projectable_rows(control, _claim_rows(control, claim))
                    projected.extend(export_projection(control, raw))
                    from drover.server.legacy_outbox import write_legacy_events

                    write_legacy_events(
                        control,
                        raw,
                        self.parquet_dir,
                        claim.batch_id,
                    )
                    receipt = publish_outbox_batch(
                        control,
                        claim,
                        parquet_dir=self.parquet_dir,
                        now=stamp,
                    )
                    published = receipt.member_count
            if published:
                self._rebuild_relation_and_acknowledge(acknowledge=False)

        # Detach control reads before projection/model-job work. Summary link
        # writes are short retry-safe effects after the analytical connection closes.
        from drover.server.memory_identity import (
            apply_memory_links,
            read_memory_sessions,
            refresh_memory_projection,
        )

        with control_plane_connection(self.control_path) as control:
            sessions = read_memory_sessions(control)
        analytics = open_duckdb_connection(self.analytical_path)
        try:
            if not self._relation_initialized:
                with control_plane_connection(self.control_path) as control:
                    register_published_harness_events_relation(analytics, control)
                self._relation_initialized = True
            if projected:
                from drover.server.ingest import _upsert_tasks
                from drover.server.rollup import rollup_tasks

                _upsert_tasks(analytics, projected)
                for date in sorted({r["date"] for r in projected}):
                    analytics.execute(
                        """INSERT INTO agent_event_partition_activity VALUES (?, ?)
                        ON CONFLICT (date) DO UPDATE SET latest_ingested_at = greatest(
                          agent_event_partition_activity.latest_ingested_at, EXCLUDED.latest_ingested_at)""",
                        [date, stamp],
                    )
            links = refresh_memory_projection(
                analytics, sessions, store_path=self.control_path
            )
            if projected:
                rollup_tasks(
                    analytics,
                    task_ids=sorted({r["task_id"] for r in projected}),
                    dates=sorted({r["date"] for r in projected}),
                )
        finally:
            analytics.close()
        with control_plane_connection(self.control_path) as control:
            apply_memory_links(control, links)
        # Summary claims become eligible only after the full legacy read projection
        # is visible; an ack before projection could expose an older generation.
        acknowledged = self._rebuild_relation_and_acknowledge()
        with control_plane_connection(self.control_path) as control:
            current = outbox_status(control)
        if current.get("stalled_published_batches", 0):
            log.warning(
                "%s published outbox batches remain unacknowledged after 5 minutes (oldest %s)",
                current["stalled_published_batches"],
                current["oldest_stalled_published_at"],
            )
        retention = self._prune_verified_payloads()
        if is_postgres_control_store(self.control_path):
            with control_plane_connection(self.control_path) as control:
                prune_acknowledged_outbox(
                    control,
                    retention_days=self.acknowledgement_retention_days,
                    limit=self.retention_limit,
                    now=stamp,
                )
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

    @staticmethod
    def _projectable_rows(control, rows):
        if not rows:
            return []
        known = {
            r[0]
            for r in control.execute(
                "SELECT session_id FROM harness_sessions WHERE session_id=ANY(?::VARCHAR[])",
                [[r["session_id"] for r in rows]],
            ).fetchall()
        }
        return [r for r in rows if r["session_id"] in known]

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

    def _rebuild_relation_and_acknowledge(self, *, acknowledge: bool = True) -> int:
        with control_plane_connection(self.control_path) as control:
            rows = control.execute(
                "SELECT batch_id FROM control_outbox_batches "
                "WHERE state = 'published' ORDER BY published_at, batch_id"
            ).fetchall()
            if not rows:
                return 0
            analytics = open_duckdb_connection(self.analytical_path)
            ready = []
            try:
                register_published_harness_events_relation(analytics, control)
                self._relation_initialized = True
                if acknowledge:
                    for (batch_id,) in rows:
                        members = control.execute(
                            """SELECT e.event_id, e.dedup_key, e.normalized_source, e.created_at
                            FROM control_outbox_batch_events b JOIN harness_events e USING(event_id)
                            WHERE b.batch_id=?""",
                            [batch_id],
                        ).fetchall()
                        harness_ids = [r[0] for r in members if r[2] != "collector"]
                        collector_keys = [r[1] for r in members if r[2] == "collector"]
                        dates = sorted(
                            {
                                r[3].date().isoformat()
                                for r in members
                                if r[2] == "collector"
                            }
                        )
                        projected = (
                            analytics.execute(
                                "SELECT count(*) FROM control_memory_events WHERE id=ANY(?::VARCHAR[])",
                                [harness_ids],
                            ).fetchone()[0]
                            if harness_ids
                            else 0
                        )
                        paths = [
                            str(p)
                            for day in dates
                            for p in (
                                self.parquet_dir / "agent_events" / f"date={day}"
                            ).glob("agent_id=*/*.parquet")
                        ]
                        native = (
                            analytics.execute(
                                "SELECT count(DISTINCT dedup_key) FROM read_parquet(?,union_by_name=true) WHERE dedup_key=ANY(?::VARCHAR[])",
                                [paths, collector_keys],
                            ).fetchone()[0]
                            if collector_keys and paths
                            else 0
                        )
                        if (
                            members
                            and projected == len(harness_ids)
                            and native == len(collector_keys)
                        ):
                            ready.append(str(batch_id))
            finally:
                analytics.close()
            acknowledged = 0
            for batch_id in ready:
                if acknowledge_outbox_batch(control, batch_id):
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
            after=self._prune_cursor,
            cursor_callback=lambda cursor: setattr(self, "_prune_cursor", cursor),
        )
