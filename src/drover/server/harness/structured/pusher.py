"""Push structured session events to central continuously.

``EventPusher`` batches events emitted by the structured session manager
(``on_message`` in ``manager.py``) and POSTs them to central's
``/harness/events`` ingest route. Delivery is at-least-once: a batch that
fails is retried, and a batch that outlives its retry attempts is *kept* and
re-offered on the next cycle rather than discarded (central is the idempotent
side, keyed by ``event_id``). Only two things lose an event -- a queue that
overflows and a shutdown central never comes back from -- and both bump
``record_undelivered_events``. Never log event text or the bearer token --
only counts.
"""

from __future__ import annotations

import json
import sys
import threading
from queue import Empty, SimpleQueue
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

_MAX_QUEUE = 5000
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 5.0
_POST_TIMEOUT_SECONDS = 10.0
# stop() must outlast one in-flight HTTP attempt (10 s) so the worker can
# hand back an undelivered batch instead of being orphaned mid-retry.
_STOP_JOIN_SECONDS = 12.0
# Final flush attempts. More than one because a worker that outlived the join
# can hand a batch back after the first sweep has already read the queue.
_SHUTDOWN_ATTEMPTS = 2


def _record_undelivered(count: int) -> None:
    # Imported here, not at module scope: daemon imports this module, so a
    # top-level import would be circular (same reason as manager.py's).
    from drover.server.harness.daemon import record_undelivered_events

    record_undelivered_events(count)


class EventPusher:
    """Batches and pushes structured session events to central."""

    def __init__(
        self,
        central_url: str,
        token: str,
        *,
        batch_interval: float = 2.0,
    ) -> None:
        self._central_url = central_url.rstrip("/")
        self._token = token
        self._batch_interval = batch_interval
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
        """POST one batch; True only for a 2xx response."""
        return self._post(batch)

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
            if not self._post(remaining):
                self._retain(remaining)
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
            if self._post(batch):
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
        """POST one batch; True only for a 2xx response.

        urllib raises HTTPError for 4xx/5xx and OSError/URLError for network
        failures, so every non-success surfaces as an exception -> False.
        """
        data = json.dumps({"events": batch}).encode("utf-8")
        request = Request(
            f"{self._central_url}/harness/events",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
        )
        with self._post_lock:
            try:
                with urlopen(request, timeout=_POST_TIMEOUT_SECONDS) as response:
                    return 200 <= response.status < 300
            except (OSError, URLError):
                return False


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
    restart-loss problem this reconciliation path exists to repair.
    """
    if pusher is None or not pusher.is_configured():
        return 0
    if not hasattr(registry, "list_events_for_reconciliation"):
        return 0
    page_size = max(1, int(batch_size))
    after_created_at = None
    after_event_id = None
    reconciled_count = 0
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
            return None
        if not events:
            return reconciled_count

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
                payload["ts"] = event.created_at.isoformat()
            records.append(payload)

        if not pusher.post_batch(records):
            return None
        reconciled_count += len(records)
        final_event = events[-1]
        if final_event.created_at is None:
            return None
        after_created_at = final_event.created_at
        after_event_id = final_event.event_id
