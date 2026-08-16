from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic, sleep

from drover.server.harness.daemon import (
    reset_undelivered_event_count,
    undelivered_event_count,
)
from drover.server.harness.structured import pusher as pusher_module
from drover.server.harness.structured.pusher import EventPusher, reconcile_unsent_events


class _FakeCentralHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    status_sequence: list[int] = []
    failing: bool = False  # while True, every POST gets a 500

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        self.__class__.requests.append(
            {
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )
        status = 500 if self.__class__.failing else 200
        if self.__class__.status_sequence:
            status = self.__class__.status_sequence.pop(0)
        events = body.get("events") or []
        payload = json.dumps({"ingested": len(events)}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if status < 300:
            self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _start_fake_central() -> ThreadingHTTPServer:
    _FakeCentralHandler.requests = []
    _FakeCentralHandler.status_sequence = []
    _FakeCentralHandler.failing = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCentralHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _wait_until(predicate, *, timeout: float) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(0.02)
    return predicate()


def test_pusher_batches_events_into_one_post():
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        pusher = EventPusher(
            f"http://127.0.0.1:{port}", "secret-token", batch_interval=0.2
        )
        pusher.start()
        pusher.push("s1", {"event_id": "e1", "type": "assistant_output", "payload": {}})
        pusher.push("s1", {"event_id": "e2", "type": "assistant_output", "payload": {}})
        assert _wait_until(lambda: len(_FakeCentralHandler.requests) >= 1, timeout=2.0)
        pusher.stop()
    finally:
        server.shutdown()
        server.server_close()

    assert len(_FakeCentralHandler.requests) == 1
    request = _FakeCentralHandler.requests[0]
    assert request["path"] == "/harness/events"
    assert request["authorization"] == "Bearer secret-token"
    events = request["body"]["events"]
    assert [event["event_id"] for event in events] == ["e1", "e2"]


def test_pusher_flushes_immediately_on_turn_complete():
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        pusher = EventPusher(
            f"http://127.0.0.1:{port}", "secret-token", batch_interval=5.0
        )
        pusher.start()
        start = monotonic()
        pusher.push("s1", {"event_id": "e1", "type": "assistant_output", "payload": {}})
        pusher.push(
            "s1",
            {"event_id": "e2", "type": "status", "payload": {"turn_complete": True}},
        )
        delivered = _wait_until(
            lambda: len(_FakeCentralHandler.requests) >= 1, timeout=1.5
        )
        elapsed = monotonic() - start
        pusher.stop()
    finally:
        server.shutdown()
        server.server_close()

    assert delivered
    assert elapsed < 1.5
    events = _FakeCentralHandler.requests[0]["body"]["events"]
    assert {event["event_id"] for event in events} == {"e1", "e2"}


def test_pusher_redelivers_same_events_after_failure_until_success():
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        _FakeCentralHandler.status_sequence = [500]
        pusher = EventPusher(
            f"http://127.0.0.1:{port}", "secret-token", batch_interval=0.2
        )
        pusher.start()
        pusher.push("s1", {"event_id": "e1", "type": "assistant_output", "payload": {}})
        assert _wait_until(lambda: len(_FakeCentralHandler.requests) >= 2, timeout=8.0)
        pusher.stop()
    finally:
        server.shutdown()
        server.server_close()

    assert len(_FakeCentralHandler.requests) == 2
    first, second = _FakeCentralHandler.requests
    assert [event["event_id"] for event in first["body"]["events"]] == ["e1"]
    assert [event["event_id"] for event in second["body"]["events"]] == ["e1"]


def test_pusher_drops_oldest_when_queue_is_full(capfd):
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        # A batch_interval long enough that nothing drains while we overfill
        # the queue -- proves push() enforces the cap itself, not the drain.
        pusher = EventPusher(
            f"http://127.0.0.1:{port}", "secret-token", batch_interval=60.0
        )
        for index in range(5002):
            pusher.push(
                "s1",
                {"event_id": f"e{index}", "type": "assistant_output", "payload": {}},
            )
        assert pusher._queue_len == 5000
        drained = pusher._drain()
    finally:
        server.shutdown()
        server.server_close()

    assert len(drained) == 5000
    assert drained[0]["event_id"] == "e2"  # e0/e1 were dropped as the oldest
    assert drained[-1]["event_id"] == "e5001"
    stderr = capfd.readouterr().err
    # Rate-limited to one line per overflow episode, and counts only.
    assert stderr.count("queue full") == 1
    assert "e0" not in stderr and "secret-token" not in stderr


def test_pusher_stop_delivers_in_flight_batch_once_central_recovers():
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        _FakeCentralHandler.failing = True
        pusher = EventPusher(
            f"http://127.0.0.1:{port}", "secret-token", batch_interval=0.1
        )
        pusher.start()
        pusher.push("s1", {"event_id": "e1", "type": "assistant_output", "payload": {}})
        # Wait for the first failed attempt so the batch is in-flight in the
        # worker (sitting in its retry backoff), then recover central and
        # stop(): the worker must hand the batch back and stop()'s final
        # flush must deliver it.
        assert _wait_until(lambda: len(_FakeCentralHandler.requests) >= 1, timeout=2.0)
        _FakeCentralHandler.failing = False
        start = monotonic()
        pusher.stop()
        stop_elapsed = monotonic() - start
    finally:
        server.shutdown()
        server.server_close()

    assert stop_elapsed < 5.0  # backoff was interrupted, not waited out
    delivered = [
        request
        for request in _FakeCentralHandler.requests
        if [event["event_id"] for event in request["body"]["events"]] == ["e1"]
    ]
    assert len(delivered) >= 2  # the failed attempt(s) plus stop()'s flush


def test_pusher_stop_drops_with_counts_only_line_when_central_stays_down(capfd):
    # Grab a port with no listener so every POST fails fast.
    probe = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCentralHandler)
    port = probe.server_address[1]
    probe.server_close()

    pusher = EventPusher(f"http://127.0.0.1:{port}", "secret-token", batch_interval=0.1)
    pusher.start()
    pusher.push(
        "s1",
        {
            "event_id": "e1",
            "type": "assistant_output",
            "text": "sensitive text",
            "payload": {},
        },
    )
    sleep(0.3)  # let the worker attempt delivery and enter its backoff
    start = monotonic()
    pusher.stop()
    stop_elapsed = monotonic() - start

    assert stop_elapsed < 5.0  # returns promptly; backoff was interrupted
    stderr = capfd.readouterr().err
    assert "dropping 1 undelivered events at shutdown" in stderr
    assert "sensitive text" not in stderr
    assert "secret-token" not in stderr


def test_pusher_keeps_a_batch_that_outlived_its_retry_attempts(monkeypatch):
    # Issue #99: ten events vanished mid-stream while the session kept
    # running. An outage longer than one cycle's attempts used to discard the
    # batch permanently, so the hub's copy of the transcript grew a hole
    # nothing could ever fill. The batch must survive to the next cycle and go
    # out when central comes back -- without stop() being what rescues it.
    monkeypatch.setattr(pusher_module, "_RETRY_BACKOFF_SECONDS", 0.0)
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        _FakeCentralHandler.failing = True
        pusher = EventPusher(
            f"http://127.0.0.1:{port}", "secret-token", batch_interval=0.05
        )
        pusher.start()
        pusher.push("s1", {"event_id": "e1", "type": "assistant_output", "payload": {}})
        assert _wait_until(
            lambda: len(_FakeCentralHandler.requests) >= pusher_module._MAX_ATTEMPTS,
            timeout=5.0,
        )
        exhausted = len(_FakeCentralHandler.requests)
        _FakeCentralHandler.failing = False
        delivered = _wait_until(
            lambda: len(_FakeCentralHandler.requests) > exhausted, timeout=5.0
        )
        last = _FakeCentralHandler.requests[-1]
        pusher.stop()
    finally:
        server.shutdown()
        server.server_close()

    assert delivered
    assert [event["event_id"] for event in last["body"]["events"]] == ["e1"]


def test_pusher_counts_events_it_can_never_deliver(capfd):
    # The loss in #99 was invisible: drover_harness_dropped_events_total sat
    # at 0 because it only covers registry writes, and nothing counted the
    # push path at all. Whatever the pusher genuinely cannot hand over must
    # move a counter.
    reset_undelivered_event_count()
    # A port with no listener, so every POST fails fast.
    probe = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCentralHandler)
    port = probe.server_address[1]
    probe.server_close()

    pusher = EventPusher(f"http://127.0.0.1:{port}", "secret-token", batch_interval=0.1)
    pusher.start()
    pusher.push("s1", {"event_id": "e1", "type": "assistant_output", "payload": {}})
    pusher.push("s1", {"event_id": "e2", "type": "assistant_output", "payload": {}})
    sleep(0.3)  # let the worker attempt delivery and enter its backoff
    pusher.stop()
    capfd.readouterr()

    assert undelivered_event_count() == 2
    reset_undelivered_event_count()


def test_pusher_reconcile_unsent_events():
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        pusher = EventPusher(f"http://127.0.0.1:{port}", "secret-token")

        class _FakeRegistry:
            def list_events_for_reconciliation(
                self, *, after_created_at=None, after_event_id=None, **kwargs
            ):
                from drover.server.harness.models import HarnessEvent

                del after_event_id, kwargs
                if after_created_at is not None:
                    return []
                created_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
                return [
                    HarnessEvent(
                        event_id="e1",
                        session_id="s1",
                        event_type="user_input",
                        payload={"text": "hi", "type": "user_input"},
                        seq=1,
                        created_at=created_at,
                    ),
                    HarnessEvent(
                        event_id="e2",
                        session_id="s1",
                        event_type="assistant_output",
                        payload={"text": "hello", "type": "assistant_output"},
                        seq=2,
                        created_at=created_at + timedelta(seconds=1),
                    ),
                ]

        count = reconcile_unsent_events(_FakeRegistry(), pusher)
        assert count == 2
        assert len(_FakeCentralHandler.requests) == 1
        request = _FakeCentralHandler.requests[0]
        assert request["path"] == "/harness/events"
        assert [e["event_id"] for e in request["body"]["events"]] == ["e1", "e2"]
    finally:
        server.shutdown()
        server.server_close()
