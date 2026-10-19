"""Push structured session events to central continuously.

``EventPusher`` batches events emitted by the structured session manager
(``on_message`` in ``manager.py``) and POSTs them to central's
``/harness/events`` ingest route. Delivery is at-least-once: a batch that
fails is retried, and a batch that outlives its retry attempts is *kept* and
re-offered on the next cycle rather than discarded (central is the idempotent
side, keyed by ``event_id``). Queue overflow and an exhausted shutdown flush
bump ``record_undelivered_events``. Authorization failures also bump the metric,
but retain events and back off until credentials recover. Never log event text
or the bearer token, only counts and retry delays.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from queue import Empty, SimpleQueue
from time import monotonic
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("drover.event_pusher")

_MAX_QUEUE = 5000
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 5.0
_AUTH_BACKOFF_SECONDS = 30.0
_AUTH_MAX_BACKOFF_SECONDS = 300.0
_POST_TIMEOUT_SECONDS = 10.0
# Keep a full MiB below the hub's 16 MiB ingress limit.  This is deliberately
# a client-side protocol limit rather than an import from web.app: harnessd
# may be older than the hub during a rolling upgrade, and must protect the hub
# without importing its server package.
_MAX_EVENT_REQUEST_BYTES = 15 * 1024 * 1024
# Live transcript deltas should not wait behind a multi-second bulk batch.
# This remains a short batching window so a token burst is posted together.
_LIVE_BATCH_INTERVAL_SECONDS = 0.25
# stop() must outlast one in-flight HTTP attempt (10 s) so the worker can
# hand back an undelivered batch instead of being orphaned mid-retry.
_STOP_JOIN_SECONDS = 12.0
# Final flush attempts. More than one because a worker that outlived the join
# can hand a batch back after the first sweep has already read the queue.
_SHUTDOWN_ATTEMPTS = 2
_REQUEST_TOO_LARGE = object()


def _record_undelivered(count: int) -> None:
    # Imported here, not at module scope: daemon imports this module, so a
    # top-level import would be circular (same reason as manager.py's).
    from drover.server.harness.daemon import record_undelivered_events

    record_undelivered_events(count)


def _record_orphaned(count: int) -> None:
    from drover.server.harness.daemon import record_orphaned_events

    record_orphaned_events(count)


class EventPusher:
    """Batches and pushes structured session events to central."""

    def __init__(
        self,
        central_url: str,
        token: str,
        *,
        batch_interval: float = _LIVE_BATCH_INTERVAL_SECONDS,
        registry: Any = None,
    ) -> None:
        self._central_url = central_url.rstrip("/")
        self._token = token
        self._batch_interval = batch_interval
        self._registry = registry
        self.reconciliation_deferred_reason: str | None = None
        self._queue: SimpleQueue[dict[str, Any]] = SimpleQueue()
        self._queue_len = 0
        self._queue_lock = threading.Lock()
        # Reconciliation and the live worker share one central projection.
        # Their requests must not overlap or a replay rebuild can race a live
        # tail update and overwrite newer derived session state.
        self._post_lock = threading.Lock()
        self._overflowing = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Events a delivery attempt could not place. Kept here (not thrown
        # away) and prepended to the next drain, so a central that is down
        # longer than one cycle's attempts costs latency, not history.
        self._unsent: list[dict[str, Any]] = []
        # Shared cooldown prevents the live worker and durable reconciliation
        # from independently hammering central while a credential is repaired.
        self._authorization_retry_at = 0.0
        self._authorization_backoff = _AUTH_BACKOFF_SECONDS
        # Ownership races delay only their own session, including durable replay.
        # Each entry holds (next attempt time, next exponential delay).
        self._pending_sessions: dict[str, tuple[float, float]] = {}
        self._orphan_logged_sessions: set[str] = set()
        # The original body of a size-capped event is intentionally not
        # delivered.  Count and warn once per identity, even if an outage
        # makes us offer its compact replacement more than once.
        self._size_capped_event_ids: set[str] = set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="drover-event-pusher", daemon=True
        )
        self._thread.start()

    def is_configured(self) -> bool:
        return bool(self._central_url and self._token)

    def is_stopping(self) -> bool:
        return self._stop.is_set()

    def post_batch(self, batch: list[dict[str, Any]]) -> bool:
        """Return True only when every event is acknowledged."""
        return self._post(batch)

    def post_batch_pending(
        self, batch: list[dict[str, Any]]
    ) -> list[dict[str, Any]] | None:
        """Return unacknowledged ownership events, or None on request failure.

        Reconciliation keeps pending events in its durable ledger and can
        continue offering later pages without copying them into the live queue.
        """
        return self._post_pending(batch)

    def reconcile(
        self,
        registry: Any,
        *,
        host_id: str | None = None,
        batch_size: int = 100,
    ) -> int | None:
        return reconcile_unsent_events(
            registry,
            self,
            host_id=host_id,
            batch_size=batch_size,
        )

    def push(self, session_id: str, event: dict[str, Any]) -> None:
        del session_id  # event already carries session_id; kept for symmetry
        with self._queue_lock:
            if self._queue_len >= _MAX_QUEUE:
                try:
                    self._queue.get_nowait()
                    self._queue_len -= 1
                except Empty:
                    pass
                if not self._overflowing:
                    self._overflowing = True
                    print(
                        "drover event pusher: queue full "
                        f"({_MAX_QUEUE}); dropping oldest events",
                        file=sys.stderr,
                    )
            self._queue.put(event)
            self._queue_len += 1
        if _is_flush_now_event(event):
            self._wake.set()

    def stop(self) -> None:
        """Stop the worker, then give undelivered events a last few attempts.

        The worker's retry backoff waits on the stop event, so it wakes
        immediately, retains any in-flight batch, and exits. Whatever remains
        (retained batch + queue remnants) is drained and re-posted; the drain
        repeats because a worker that outlived the join can retain a batch
        after the first sweep has already read the queue. Only what is still
        unsent when the attempts run out is lost, and it is counted as well as
        logged -- nothing disappears silently.
        """
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=_STOP_JOIN_SECONDS)
        for _ in range(_SHUTDOWN_ATTEMPTS):
            remaining = self._drain()
            if not remaining:
                break
            pending = self._post_pending(remaining)
            self._retain(remaining if pending is None else pending)
        lost = self._drain()
        if lost:
            _record_undelivered(len(lost))
            print(
                f"drover event pusher: dropping {len(lost)} undelivered "
                "events at shutdown",
                file=sys.stderr,
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            cooldown = self._authorization_retry_at - monotonic()
            if cooldown > 0 and self._stop.wait(cooldown):
                break
            self._wake.wait(self._batch_interval)
            self._wake.clear()
            batch = self._drain()
            if batch:
                self._deliver_with_retries(batch)

    def _drain(self) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except Empty:
                break
        with self._queue_lock:
            if batch:
                self._queue_len = max(0, self._queue_len - len(batch))
                self._overflowing = False
            retained, self._unsent = self._unsent, []
        # Retained first: seq order across the batch is preserved, which is
        # what the hub replays a transcript in.
        return retained + batch

    def _retain(self, batch: list[dict[str, Any]]) -> None:
        """Keep an undelivered batch for the next drain instead of losing it.

        Re-offering is safe: central is idempotent on ``event_id``, so a
        record it already stored costs a skipped insert, never a duplicate.
        Bounded by the same ``_MAX_QUEUE`` cap as the inbound queue, since an
        outage that outlasts the cap must not grow the process without limit
        -- the oldest go, and those are counted, because that is the one live
        case where an event genuinely cannot be delivered.
        """
        with self._queue_lock:
            self._unsent.extend(batch)
            overflow = len(self._unsent) + self._queue_len - _MAX_QUEUE
            lost = min(max(0, overflow), len(self._unsent))
            if lost:
                del self._unsent[:lost]
        if lost:
            _record_undelivered(lost)
            print(
                f"drover event pusher: dropping {lost} undelivered events; "
                f"retry buffer full ({_MAX_QUEUE})",
                file=sys.stderr,
            )

    def _deliver_with_retries(self, batch: list[dict[str, Any]]) -> None:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            pending = self._post_pending(batch)
            if pending is not None:
                self._retain(pending)
                return
            if self._authorization_retry_at > monotonic():
                # One authorization attempt per backoff step. Re-offer this
                # batch next cycle instead of spending the normal short retries.
                self._retain(batch)
                return
            if self._stop.is_set():
                # Shutdown in progress: hand the batch to stop()'s final
                # flush instead of burning the remaining attempts here.
                self._retain(batch)
                return
            if attempt < _MAX_ATTEMPTS:
                # Interruptible backoff: stop() wakes this immediately.
                if self._stop.wait(_RETRY_BACKOFF_SECONDS):
                    self._retain(batch)
                    return
        # Exhausting the attempts is not the end of the road. Dropping here is
        # what lost ten mid-stream events in #99: an outage a few seconds
        # longer than one cycle punched a permanent hole in the hub's copy,
        # uncounted. Keep the batch and re-offer it next cycle instead.
        self._retain(batch)

    def _post(self, batch: list[dict[str, Any]]) -> bool:
        return self._post_pending(batch) == []

    def _post_pending(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """POST eligible sessions once, preserving the input order for retries."""
        with self._post_lock:
            if self._authorization_retry_at > monotonic():
                return None
            try:
                quarantined = (
                    self._registry.quarantined_event_ids(
                        [str(e.get("event_id") or "") for e in batch]
                    )
                    if self._registry is not None
                    else set()
                )
            except Exception:
                self.reconciliation_deferred_reason = "local quarantine unavailable"
                return None
            batch = [e for e in batch if e.get("event_id") not in quarantined]
            held_sessions = {
                session_id
                for session_id, (retry_at, _) in self._pending_sessions.items()
                if retry_at > monotonic()
            }
            offered = [e for e in batch if e.get("session_id") not in held_sessions]
            if not offered:
                return list(batch)
            pending_sessions: set[str] = set()
            pages = self._bounded_pages(offered)
            while pages:
                page = pages.pop(0)
                # A previous page can discover that this session is not yet
                # owned by the hub.  Do not offer a later fragment of it.
                page = [
                    event
                    for event in page
                    if event.get("session_id") not in pending_sessions
                ]
                if not page:
                    continue
                result = self._post_page(page)
                if result is _REQUEST_TOO_LARGE:
                    if len(page) > 1:
                        midpoint = len(page) // 2
                        pages[0:0] = [page[:midpoint], page[midpoint:]]
                        continue
                    event = page[0]
                    event_id = str(event.get("event_id") or "")
                    if event_id in self._size_capped_event_ids:
                        # A hub with a smaller limit than even the compact
                        # identity envelope cannot receive this record.  Its
                        # body was already counted and warned about when it
                        # was capped; make that loss terminal so it cannot
                        # hold every following durable page hostage forever.
                        log.warning("structured event rejected after size cap")
                        continue
                    pages.insert(0, [self._size_cap_event(event)])
                    continue
                if result is None:
                    return None
                page_pending, orphaned_sessions, rejected = result
                offered_sessions = {event.get("session_id") for event in page}
                if not page_pending <= offered_sessions:
                    return None
                if not orphaned_sessions <= offered_sessions - page_pending:
                    return None
                if not set(rejected) <= offered_sessions - page_pending:
                    return None
                # Older hubs use only `orphaned`; keep that terminal reply
                # distinct from an acknowledgement as well.
                for session_id in orphaned_sessions:
                    rejected.setdefault(
                        session_id, "session_missing_after_registration_window"
                    )
                for session_id, reason in rejected.items():
                    rejected_events = [
                        e for e in page if e.get("session_id") == session_id
                    ]
                    if self._registry is None:
                        # Never acknowledge rejected durable history unless its
                        # quarantine receipt was committed first.
                        return None
                    try:
                        self._registry.quarantine_events(rejected_events, reason)
                    except Exception:
                        self.reconciliation_deferred_reason = (
                            "local quarantine unavailable"
                        )
                        return None
                    log.warning(
                        "structured events quarantined: session_id=%s events=%d reason=%s",
                        session_id,
                        len(rejected_events),
                        reason,
                    )
                for session_id in orphaned_sessions:
                    count = sum(event.get("session_id") == session_id for event in page)
                    _record_orphaned(count)
                    if session_id in self._orphan_logged_sessions:
                        continue
                    self._orphan_logged_sessions.add(session_id)
                    log.info("structured event orphaned: events=%d", count)
                for session_id in offered_sessions - page_pending:
                    self._pending_sessions.pop(session_id, None)
                for session_id in page_pending:
                    _, delay = self._pending_sessions.get(
                        session_id, (0.0, _AUTH_BACKOFF_SECONDS)
                    )
                    self._pending_sessions[session_id] = (
                        monotonic() + delay,
                        min(delay * 2, _AUTH_MAX_BACKOFF_SECONDS),
                    )
                    count = sum(event.get("session_id") == session_id for event in page)
                    _record_undelivered(count)
                    log.warning(
                        "structured event ownership backoff: events=%d retry_seconds=%.0f",
                        count,
                        delay,
                    )
                pending_sessions.update(page_pending)
            return [
                e
                for e in batch
                if e.get("session_id") in held_sessions | pending_sessions
            ]

    @staticmethod
    def _request_body(events: list[dict[str, Any]]) -> bytes:
        return json.dumps({"events": events}).encode("utf-8")

    def _bounded_pages(
        self, events: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        """Split outgoing records by their encoded JSON size as well as count."""
        pages: list[list[dict[str, Any]]] = []
        page: list[dict[str, Any]] = []
        for event in events:
            candidate = page + [event]
            if len(self._request_body(candidate)) <= _MAX_EVENT_REQUEST_BYTES:
                page = candidate
                continue
            if page:
                pages.append(page)
            page = [event]
            if len(self._request_body(page)) > _MAX_EVENT_REQUEST_BYTES:
                page = [self._size_cap_event(event)]
            pages.append(page)
            page = []
        if page:
            pages.append(page)
        return pages

    def _size_cap_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Keep an oversized record's identity while dropping its huge body.

        The hub keys ingest idempotency on ``event_id`` and retains ``seq`` in
        its ledger.  Sending this compact tombstone is therefore preferable to
        letting one unrepresentable tool output permanently hold every later
        durable replay page hostage.
        """
        original_bytes = len(self._request_body([event]))
        event_id = str(event.get("event_id") or "")
        if event_id not in self._size_capped_event_ids:
            self._size_capped_event_ids.add(event_id)
            _record_undelivered(1)
            log.warning(
                "structured event size capped: original_bytes=%d",
                original_bytes,
            )
        capped: dict[str, Any] = {
            "event_id": event_id,
            "session_id": str(event.get("session_id") or ""),
            "type": str(event.get("type") or "unknown"),
            "seq": event.get("seq"),
            "payload": {
                "truncated": True,
                "reason": "event exceeds harness request byte limit",
                "original_bytes": original_bytes,
            },
        }
        timestamp = event.get("ts")
        if isinstance(timestamp, str) and len(timestamp) <= 256:
            capped["ts"] = timestamp
        return capped

    def _post_page(
        self, events: list[dict[str, Any]]
    ) -> tuple[set[str], set[str], dict[str, str]] | object | None:
        request = Request(
            f"{self._central_url}/harness/events",
            data=self._request_body(events),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
                "X-Drover-Event-Delivery": "quarantine-v1",
            },
        )
        try:
            with urlopen(request, timeout=_POST_TIMEOUT_SECONDS) as response:
                if not 200 <= response.status < 300:
                    return None
                pending_sessions, orphaned_sessions, rejected = (
                    self._read_acknowledgement(response)
                )
                self._authorization_retry_at = 0.0
                self._authorization_backoff = _AUTH_BACKOFF_SECONDS
                return pending_sessions, orphaned_sessions, rejected
        except HTTPError as exc:
            if exc.code == 413:
                return _REQUEST_TOO_LARGE
            if exc.code != 403:
                return None
            try:
                pending_sessions, orphaned, rejected = self._read_acknowledgement(exc)
            except (OSError, ValueError):
                pending_sessions = set()
                orphaned, rejected = set(), {}
            offered = {event.get("session_id") for event in events}
            if (
                rejected
                and set(rejected) == offered
                and not pending_sessions
                and not orphaned
            ):
                return set(), set(), rejected
            # Only the explicit all-pending ownership response is a partial
            # acknowledgement on 403. Other failures retain every record.
            if pending_sessions != {event.get("session_id") for event in events}:
                pending_sessions = set()
            if pending_sessions:
                return pending_sessions, set(), {}
            delay = self._authorization_backoff
            self._authorization_retry_at = monotonic() + delay
            self._authorization_backoff = min(delay * 2, _AUTH_MAX_BACKOFF_SECONDS)
            _record_undelivered(len(events))
            log.warning(
                "structured event authorization backoff: events=%d retry_seconds=%.0f",
                len(events),
                delay,
            )
            return None
        except (OSError, URLError, ValueError):
            return None

    @staticmethod
    def _read_acknowledgement(
        response: Any,
    ) -> tuple[set[str], set[str], dict[str, str]]:
        raw = response.read()
        if not raw:
            return set(), set(), {}
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("invalid event acknowledgement")
        pending = body.get("pending", [])
        if not isinstance(pending, list) or not all(
            isinstance(s, str) for s in pending
        ):
            raise ValueError("invalid pending session acknowledgement")
        orphaned = body.get("orphaned", [])
        if not isinstance(orphaned, list) or not all(
            isinstance(session_id, str) for session_id in orphaned
        ):
            raise ValueError("invalid orphaned session acknowledgement")
        rejected = body.get("rejected", {})
        reasons = {
            "session_missing_after_registration_window",
            "session_owned_by_another_host",
        }
        if not isinstance(rejected, dict) or not all(
            isinstance(s, str) and isinstance(reason, str) and reason in reasons
            for s, reason in rejected.items()
        ):
            raise ValueError("invalid rejected session acknowledgement")
        return set(pending), set(orphaned), rejected

    @staticmethod
    def _read_pending(response: Any) -> set[str]:
        """Compatibility helper for focused callers that only need pending ids."""
        return EventPusher._read_acknowledgement(response)[0]


def _is_flush_now_event(event: dict[str, Any]) -> bool:
    if event.get("type") != "status":
        return False
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("turn_complete")) or "exited" in payload


def reconcile_unsent_events(
    registry: Any,
    pusher: EventPusher | None,
    *,
    host_id: str | None = None,
    batch_size: int = 100,
) -> int | None:
    """Offer every durable structured event to central in bounded pages.

    Repairs any event gaps caused by an unexpected harnessd restart
    (crash, reboot, deploy). Central /harness/events is idempotent on
    event_id, so re-sending durable events is safe and duplicate-free.

    Returns the number of events offered after a complete pass, or ``None``
    when the pass could not finish. Failed pages stay only in DuckDB: copying
    them into EventPusher's bounded in-memory retry buffer would recreate the
    restart-loss problem this reconciliation path exists to repair. Pending
    ownership pages do not stop later pages, but keep the pass incomplete so
    the daemon will revisit them after registration or orphan expiry.
    """
    # Imported lazily: registry pulls this module in at call time, so a
    # module-level import here would close the cycle.
    from drover.server.harness.registry import _db_timestamp_to_utc

    if pusher is None or not pusher.is_configured():
        return 0
    pusher._registry = registry if hasattr(registry, "quarantine_events") else None
    pusher.reconciliation_deferred_reason = None
    if not hasattr(registry, "list_events_for_reconciliation"):
        return 0
    page_size = max(1, int(batch_size))
    after_created_at = None
    after_event_id = None
    reconciled_count = 0
    ownership_pending = False
    while True:
        if getattr(pusher, "is_stopping", lambda: False)():
            return None
        try:
            events = registry.list_events_for_reconciliation(
                host_id=host_id,
                after_created_at=after_created_at,
                after_event_id=after_event_id,
                limit=page_size,
            )
        except Exception:
            pusher.reconciliation_deferred_reason = "local ledger unavailable"
            return None
        if not events:
            if ownership_pending:
                pusher.reconciliation_deferred_reason = "session ownership pending"
            return None if ownership_pending else reconciled_count

        records: list[dict[str, Any]] = []
        for event in events:
            try:
                payload = event.wire_payload()
            except Exception:
                payload = dict(event.payload) if isinstance(event.payload, dict) else {}
                payload["event_id"] = event.event_id
                payload["session_id"] = event.session_id
                if event.seq is not None:
                    payload["seq"] = event.seq
            if "type" not in payload or not payload["type"]:
                payload["type"] = event.event_type
            if "ts" not in payload and event.created_at is not None:
                # `created_at` comes straight out of a DuckDB TIMESTAMP column,
                # so it is naive *local* wall time. Send an offset-carrying ISO
                # string: the hub parses a naive `ts` as UTC, which would move
                # every reconciled event by one UTC offset.
                payload["ts"] = _db_timestamp_to_utc(event.created_at).isoformat()
            records.append(payload)

        if getattr(pusher, "is_stopping", lambda: False)():
            return None
        partial_post = getattr(pusher, "post_batch_pending", None)
        if partial_post is not None:
            pending = partial_post(records)
            if pending is None:
                pusher.reconciliation_deferred_reason = (
                    pusher.reconciliation_deferred_reason or "event delivery failed"
                )
                return None
            ownership_pending = ownership_pending or bool(pending)
            pending_ids = {str(event.get("event_id") or "") for event in pending}
            acknowledged_ids = [
                event.event_id for event in events if event.event_id not in pending_ids
            ]
            marker = getattr(registry, "mark_events_reconciled", None)
            if marker is not None:
                try:
                    marker(acknowledged_ids)
                except Exception:
                    pusher.reconciliation_deferred_reason = "local ledger update failed"
                    return None
        elif not pusher.post_batch(records):
            return None
        elif hasattr(registry, "mark_events_reconciled"):
            try:
                registry.mark_events_reconciled([event.event_id for event in events])
            except Exception:
                pusher.reconciliation_deferred_reason = "local ledger update failed"
                return None
        reconciled_count += len(records)
        final_event = events[-1]
        if final_event.created_at is None:
            return None
        after_created_at = final_event.created_at
        after_event_id = final_event.event_id
