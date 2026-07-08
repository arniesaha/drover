"""Push structured session events to central continuously.

``EventPusher`` batches events emitted by the structured session manager
(``on_message`` in ``manager.py``) and POSTs them to central's
``/harness/events`` ingest route. Delivery is best-effort and at-least-once:
a batch that fails to deliver is retried a bounded number of times and then
dropped (central is the idempotent side, keyed by ``event_id``). Never log
event text or the bearer token -- only counts.
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
        self._overflowing = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Undelivered batch handed back by the worker when stop() interrupts
        # its retry cycle; stop()'s final flush picks it up.
        self._pending_batch: list[dict[str, Any]] | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="drover-event-pusher", daemon=True
        )
        self._thread.start()

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
        """Stop the worker, then give undelivered events one last attempt.

        The worker's retry backoff waits on the stop event, so it wakes
        immediately, hands back any in-flight batch via _pending_batch, and
        exits. Whatever remains (handed-back batch + queue remnants) gets one
        final synchronous delivery attempt; on failure it is dropped with a
        counts-only stderr line -- nothing disappears silently.
        """
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=_STOP_JOIN_SECONDS)
        remaining = (self._pending_batch or []) + self._drain()
        self._pending_batch = None
        if remaining and not self._post(remaining):
            print(
                f"drover event pusher: dropping {len(remaining)} undelivered "
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
        if batch:
            with self._queue_lock:
                self._queue_len = max(0, self._queue_len - len(batch))
                self._overflowing = False
        return batch

    def _deliver_with_retries(self, batch: list[dict[str, Any]]) -> None:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            if self._post(batch):
                return
            if self._stop.is_set():
                # Shutdown in progress: hand the batch to stop()'s final
                # flush instead of burning the remaining attempts here.
                self._pending_batch = batch
                return
            if attempt < _MAX_ATTEMPTS:
                # Interruptible backoff: stop() wakes this immediately.
                if self._stop.wait(_RETRY_BACKOFF_SECONDS):
                    self._pending_batch = batch
                    return
        print(
            f"drover event pusher: dropping {len(batch)} events after "
            f"{_MAX_ATTEMPTS} failed delivery attempts",
            file=sys.stderr,
        )

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
