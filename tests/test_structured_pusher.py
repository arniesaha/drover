from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic, sleep

from drover.server.harness.daemon import (
    orphaned_event_count,
    reset_orphaned_event_count,
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


def test_pusher_default_interval_delivers_live_events_promptly():
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        pusher = EventPusher(f"http://127.0.0.1:{port}", "secret-token")
        pusher.start()
        start = monotonic()
        pusher.push("s1", {"event_id": "e1", "type": "assistant_output", "payload": {}})
        delivered = _wait_until(
            lambda: len(_FakeCentralHandler.requests) == 1, timeout=1.0
        )
        elapsed = monotonic() - start
        pusher.stop()
    finally:
        server.shutdown()
        server.server_close()

    assert delivered
    assert elapsed < 1.0


def test_pusher_splits_a_413_page_and_retries_each_half(monkeypatch):
    """A lower-limit/newer hub must not let one rejected page halt replay."""
    from io import BytesIO
    from urllib.error import HTTPError

    calls: list[list[dict]] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ingested": 1}'

    def post(request, *, timeout):
        del timeout
        events = json.loads(request.data)["events"]
        calls.append(events)
        if len(events) > 1:
            raise HTTPError(request.full_url, 413, "too large", {}, BytesIO(b"{}"))
        return Response()

    monkeypatch.setattr(pusher_module, "urlopen", post)
    pusher = EventPusher("http://central.invalid", "test-token")
    batch = [
        {"event_id": f"e{number}", "session_id": f"s{number}", "type": "status"}
        for number in range(4)
    ]

    assert pusher.post_batch(batch)
    assert [len(events) for events in calls] == [4, 2, 1, 1, 2, 1, 1]
    assert [event["event_id"] for index in (2, 3, 5, 6) for event in calls[index]] == [
        "e0",
        "e1",
        "e2",
        "e3",
    ]


def test_pusher_size_caps_one_event_without_blocking_later_events(monkeypatch, caplog):
    """One huge tool result is represented by its identity, then replay moves on."""
    calls: list[list[dict]] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ingested": 1}'

    def post(request, *, timeout):
        del timeout
        calls.append(json.loads(request.data)["events"])
        return Response()

    monkeypatch.setattr(pusher_module, "urlopen", post)
    monkeypatch.setattr(pusher_module, "_MAX_EVENT_REQUEST_BYTES", 512)
    pusher = EventPusher("http://central.invalid", "test-token")
    huge = {
        "event_id": "huge",
        "session_id": "s1",
        "type": "tool_result",
        "seq": 9,
        "payload": {"text": "x" * 4096},
    }
    later = {"event_id": "later", "session_id": "s2", "type": "status", "seq": 1}
    reset_undelivered_event_count()
    try:
        assert pusher.post_batch_pending([huge, later]) == []
        assert [event["event_id"] for page in calls for event in page] == [
            "huge",
            "later",
        ]
        delivered_huge = calls[0][0]
        assert delivered_huge["seq"] == 9
        assert delivered_huge["payload"]["truncated"] is True
        assert undelivered_event_count() == 1
        assert "structured event size capped: original_bytes=" in caplog.text
    finally:
        reset_undelivered_event_count()


def test_rejected_size_capped_event_does_not_block_later_events(monkeypatch, caplog):
    """Even a hub below the compact-envelope size cannot stall the replay."""
    from io import BytesIO
    from urllib.error import HTTPError

    calls: list[list[dict]] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ingested": 1}'

    def post(request, *, timeout):
        del timeout
        events = json.loads(request.data)["events"]
        calls.append(events)
        if events[0]["event_id"] == "huge":
            raise HTTPError(request.full_url, 413, "too large", {}, BytesIO(b"{}"))
        return Response()

    monkeypatch.setattr(pusher_module, "urlopen", post)
    monkeypatch.setattr(pusher_module, "_MAX_EVENT_REQUEST_BYTES", 512)
    pusher = EventPusher("http://central.invalid", "test-token")
    huge = {
        "event_id": "huge",
        "session_id": "s1",
        "type": "tool_result",
        "seq": 9,
        "payload": {"text": "x" * 4096},
    }
    later = {"event_id": "later", "session_id": "s2", "type": "status", "seq": 1}
    reset_undelivered_event_count()
    try:
        assert pusher.post_batch_pending([huge, later]) == []
        assert [event["event_id"] for page in calls for event in page] == [
            "huge",
            "later",
        ]
        assert undelivered_event_count() == 1
        assert "structured event rejected after size cap" in caplog.text
    finally:
        reset_undelivered_event_count()


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


def test_pusher_serializes_reconciliation_and_live_posts(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    calls = 0
    active = 0
    max_active = 0

    class _Response:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def blocking_urlopen(_request, *, timeout):
        del timeout
        nonlocal calls, active, max_active
        with guard:
            calls += 1
            active += 1
            max_active = max(max_active, active)
            call_number = calls
        if call_number == 1:
            entered.set()
            release.wait(timeout=2.0)
        with guard:
            active -= 1
        return _Response()

    monkeypatch.setattr(pusher_module, "urlopen", blocking_urlopen)
    pusher = EventPusher("http://central.invalid", "secret-token")
    first = threading.Thread(target=lambda: pusher.post_batch([{"event_id": "old"}]))
    second = threading.Thread(target=lambda: pusher.post_batch([{"event_id": "live"}]))

    first.start()
    assert entered.wait(timeout=1.0)
    second.start()
    sleep(0.1)
    try:
        assert calls == 1
    finally:
        release.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)

    assert calls == 2
    assert max_active == 1


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


def test_forbidden_batch_delivers_after_credential_recovery(monkeypatch, caplog):
    server = _start_fake_central()
    _FakeCentralHandler.status_sequence = [403, 200]
    now = [100.0]
    monkeypatch.setattr(pusher_module, "monotonic", lambda: now[0], raising=False)
    pusher = EventPusher(f"http://127.0.0.1:{server.server_port}", "old-token")
    batch = [
        {
            "event_id": "e1",
            "session_id": "s1",
            "type": "assistant_output",
            "text": "private text",
        }
    ]
    reset_undelivered_event_count()
    try:
        pusher._deliver_with_retries(batch)
        retained = pusher._drain()
        assert retained == batch
        assert len(_FakeCentralHandler.requests) == 1
        assert undelivered_event_count() == 1
        pusher._token = "fixed-token"
        now[0] += 301
        pusher._deliver_with_retries(retained)
        assert pusher._drain() == []
        assert len(_FakeCentralHandler.requests) == 2
        assert _FakeCentralHandler.requests[-1]["body"]["events"] == batch
        assert _FakeCentralHandler.requests[-1]["authorization"] == "Bearer fixed-token"
        # Recovery resets the next authorization delay to the initial step.
        _FakeCentralHandler.status_sequence = [403, 200]
        assert not pusher.post_batch(batch)
        now[0] += 29
        assert not pusher.post_batch(batch)
        assert len(_FakeCentralHandler.requests) == 3
        now[0] += 1
        assert pusher.post_batch(batch)
        assert len(_FakeCentralHandler.requests) == 4
        assert "private text" not in caplog.text
        assert "old-token" not in caplog.text and "fixed-token" not in caplog.text
    finally:
        server.shutdown()
        server.server_close()
        reset_undelivered_event_count()


def test_repeated_forbidden_responses_back_off_and_cap(monkeypatch, caplog):
    server = _start_fake_central()
    _FakeCentralHandler.status_sequence = [403] * 7
    now = [100.0]
    monkeypatch.setattr(pusher_module, "monotonic", lambda: now[0], raising=False)
    pusher = EventPusher(f"http://127.0.0.1:{server.server_port}", "test-token")
    batch = [{"event_id": "e1", "session_id": "s1", "type": "status"}]
    try:
        for step, delay in enumerate([30, 60, 120, 240, 300, 300], start=1):
            assert not pusher.post_batch(batch)
            assert len(_FakeCentralHandler.requests) == step
            now[0] += delay - 1
            for _ in range(5):
                assert not pusher.post_batch(batch)
            assert len(_FakeCentralHandler.requests) == step
            now[0] += 1
        assert caplog.text.count("authorization backoff") == 6
    finally:
        server.shutdown()
        server.server_close()
        reset_undelivered_event_count()


def test_reconciliation_retries_forbidden_page_and_preserves_local_ledger(
    tmp_path, caplog, monkeypatch
):
    from drover.schema import bootstrap
    from drover.server.harness.registry import HarnessRegistry

    path = tmp_path / "local.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    registry = HarnessRegistry(path)
    registry.create_session(
        session_id="s1",
        host_id="h1",
        harness="claude-code",
        command="claude",
        mode="structured",
    )
    registry.append_event(
        session_id="s1",
        event_type="user_input",
        payload={"text": "hello"},
        seq=1,
        event_id="e1",
        normalized_source="structured",
    )
    server = _start_fake_central()
    _FakeCentralHandler.status_sequence = [403]
    pusher = EventPusher(f"http://127.0.0.1:{server.server_port}", "test-token")
    try:
        now = [100.0]
        monkeypatch.setattr(pusher_module, "monotonic", lambda: now[0], raising=False)
        assert reconcile_unsent_events(registry, pusher, host_id="h1") is None
        # A pre-#669 hub returns a bare 403.  It remains a retry, not a
        # terminal acknowledgement that can clear the durable ledger.
        assert [
            event.event_id
            for event in registry.list_events_for_reconciliation(host_id="h1")
        ] == ["e1"]
        assert reconcile_unsent_events(registry, pusher, host_id="h1") is None
        assert len(_FakeCentralHandler.requests) == 1
        now[0] += 301
        assert reconcile_unsent_events(registry, pusher, host_id="h1") == 1
        assert len(_FakeCentralHandler.requests) == 2
        assert [event.event_id for event in registry.list_events_after("s1", 0)] == [
            "e1"
        ]
        assert caplog.text.count("authorization backoff") == 1
        assert registry.list_events_for_reconciliation(host_id="h1") == []
    finally:
        server.shutdown()
        server.server_close()
        reset_undelivered_event_count()


def test_terminal_orphan_is_marked_once_without_warning_spam(
    tmp_path, monkeypatch, caplog
):
    from drover.schema import bootstrap
    from drover.server.harness.registry import HarnessRegistry

    path = tmp_path / "local.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    registry = HarnessRegistry(path)
    registry.create_session(
        session_id="orphan", host_id="h1", harness="claude", command="claude"
    )
    for seq in (1, 2):
        registry.append_event(
            session_id="orphan",
            event_type="assistant_output",
            payload={"text": "hello"},
            seq=seq,
            event_id=f"orphan-{seq}",
            normalized_source="structured",
        )
    now = [100.0]
    monkeypatch.setattr(pusher_module, "monotonic", lambda: now[0])

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ingested": 0, "orphaned": ["orphan"]}'

    calls = []

    def post(request, *, timeout):
        calls.append(json.loads(request.data)["events"])
        return Response()

    monkeypatch.setattr(pusher_module, "urlopen", post)
    pusher = EventPusher("http://central.invalid", "token")
    caplog.set_level(logging.INFO, logger="drover.event_pusher")
    reset_orphaned_event_count()
    try:
        assert reconcile_unsent_events(registry, pusher, host_id="h1") == 2
        assert registry.list_events_for_reconciliation(host_id="h1") == []
        assert len(calls) == 1
        assert orphaned_event_count() == 2
        assert caplog.text.count("structured event orphaned") == 1
        assert "ownership backoff" not in caplog.text
    finally:
        reset_orphaned_event_count()


def test_live_worker_waits_through_authorization_backoff(monkeypatch, caplog):
    monkeypatch.setattr(pusher_module, "_AUTH_BACKOFF_SECONDS", 0.15)
    monkeypatch.setattr(pusher_module, "_AUTH_MAX_BACKOFF_SECONDS", 0.3)
    server = _start_fake_central()
    _FakeCentralHandler.status_sequence = [403, 403, 403, 200]
    pusher = EventPusher(
        f"http://127.0.0.1:{server.server_port}", "test-token", batch_interval=0.01
    )
    timestamps = []
    real_urlopen = pusher_module.urlopen

    def timed_urlopen(*args, **kwargs):
        timestamps.append(monotonic())
        return real_urlopen(*args, **kwargs)

    monkeypatch.setattr(pusher_module, "urlopen", timed_urlopen)
    batch = {
        "event_id": "e1",
        "session_id": "s1",
        "type": "status",
        "payload": {"turn_complete": True},
    }
    reset_undelivered_event_count()
    try:
        pusher.start()
        pusher.push("s1", batch)
        assert _wait_until(lambda: len(_FakeCentralHandler.requests) == 1, timeout=2)
        # New completion events must not bypass the shared cooldown.
        pusher.push("s1", dict(batch, event_id="e2"))
        assert _wait_until(lambda: len(_FakeCentralHandler.requests) >= 4, timeout=3)
        pusher.stop()
        assert len(timestamps) == 4
        assert timestamps[1] - timestamps[0] >= 0.15
        assert timestamps[2] - timestamps[1] >= 0.3
        assert timestamps[3] - timestamps[2] >= 0.3
        assert [
            event["event_id"]
            for event in _FakeCentralHandler.requests[-1]["body"]["events"]
        ] == ["e1", "e2"]
        assert caplog.text.count("authorization backoff") == 3
    finally:
        pusher.stop()
        server.shutdown()
        server.server_close()
        reset_undelivered_event_count()


def test_pending_session_backoff_does_not_block_other_sessions(monkeypatch, caplog):
    from io import BytesIO
    from urllib.error import HTTPError

    now = [100.0]
    monkeypatch.setattr(pusher_module, "monotonic", lambda: now[0])
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ingested": 1}'

    def post(request, *, timeout):
        events = json.loads(request.data)["events"]
        calls.append(events)
        if events[0]["session_id"] == "pending-session":
            raise HTTPError(
                request.full_url,
                403,
                "pending",
                {},
                BytesIO(b'{"pending": ["pending-session"]}'),
            )
        return Response()

    monkeypatch.setattr(pusher_module, "urlopen", post)
    pusher = EventPusher("http://central.invalid", "private-token")
    pending = [
        {
            "session_id": "pending-session",
            "event_id": "pending-event",
            "seq": 1,
            "text": "private-text",
        }
    ]
    live = [{"session_id": "live-session", "event_id": "live-event", "seq": 1}]
    reset_undelivered_event_count()
    try:
        for delay in (30, 60, 120, 240, 300, 300):
            count = len(calls)
            assert pusher.post_batch_pending(pending) == pending
            assert len(calls) == count + 1
            assert pusher._pending_sessions["pending-session"][0] == now[0] + delay
            assert pusher.post_batch_pending(pending + live) == pending
            assert calls[-1] == live
            assert pusher.post_batch_pending(pending) == pending
            assert len(calls) == count + 2  # cooldown sends only the live session
            now[0] += delay
        assert undelivered_event_count() == 6
        assert caplog.text.count("ownership backoff") == 6
        for private in ("private-token", "private-text", "pending-session"):
            assert private not in caplog.text
    finally:
        reset_undelivered_event_count()
