"""Durable, credential-scoped work-item subscriber events."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from test_hub_work_items import _register
from test_work_queue_execution import service
from test_work_queue_transport import transport

from drover.server.work_queue.work_item_events import WorkItemEventWorker


class _Receiver(BaseHTTPRequestHandler):
    status = 204
    received: list[tuple[str, bytes, str]] = []
    lock = threading.Lock()

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        with self.lock:
            self.received.append(
                (
                    self.headers["X-Drover-Event-Id"],
                    body,
                    self.headers["X-Drover-Signature"],
                )
            )
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def receiver():
    _Receiver.received = []
    _Receiver.status = 204
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/events"
    server.shutdown()
    server.server_close()


def _item(service):
    _register(service, session_id="events-work")
    return service.store.hub_items()[0]


def _due(service):
    with service.store.connection() as con:
        con.execute("UPDATE work_item_event_deliveries SET next_attempt_at = now()")


def test_signed_at_least_once_delivery_uses_stable_event_id(service, receiver):
    item = _item(service)
    subscription = service.store.create_work_item_subscription(
        "credential-a", url=receiver, event_kinds=["updated"]
    )
    service.store.patch_hub_item(item["id"], next_action="wait for CI")
    worker = WorkItemEventWorker(service.store, service.config.work_item_events)

    _Receiver.status = 500
    assert worker.process_once() == 1
    _due(service)
    _Receiver.status = 204
    assert worker.process_once() == 1

    assert len(_Receiver.received) == 2
    event_ids = {record[0] for record in _Receiver.received}
    assert len(event_ids) == 1  # receiver can use this value as its dedup key
    for event_id, body, signature in _Receiver.received:
        assert json.loads(body)["event_id"] == event_id
        expected = hmac.new(
            subscription["secret"].encode(), body, hashlib.sha256
        ).hexdigest()
        assert hmac.compare_digest(signature, "sha256=" + expected)


def test_backoff_dead_letters_after_bounded_attempts(service, receiver):
    item = _item(service)
    service.store.create_work_item_subscription(
        "credential-a", url=receiver, event_kinds=["updated"]
    )
    service.store.patch_hub_item(item["id"], next_action="wait for CI")
    worker = WorkItemEventWorker(
        service.store,
        service.config.work_item_events.model_copy(update={"max_attempts": 3}),
    )
    _Receiver.status = 503
    worker.process_once()
    with service.store.connection() as con:
        first_delay = con.execute(
            """SELECT EXTRACT(EPOCH FROM (next_attempt_at - now()))
               FROM work_item_event_deliveries"""
        ).fetchone()[0]
    assert 0 < first_delay <= 2
    _due(service)
    worker.process_once()
    with service.store.connection() as con:
        second_delay = con.execute(
            """SELECT EXTRACT(EPOCH FROM (next_attempt_at - now()))
               FROM work_item_event_deliveries"""
        ).fetchone()[0]
    assert 1 < second_delay <= 3
    _due(service)
    worker.process_once()

    health = service.store.work_item_delivery_health()
    assert health["dead_lettered_total"] == 1
    assert worker.health()["dead_lettered"] == 1


def test_pull_cursor_has_no_gaps_or_duplicates_during_writes(service):
    item = _item(service)
    start = threading.Event()

    def write(index):
        start.wait()
        service.store.patch_hub_item(item["id"], next_action=f"next-{index}")

    with ThreadPoolExecutor(max_workers=4) as writers:
        futures = [writers.submit(write, index) for index in range(12)]
        start.set()
        cursor = 0
        observed = []
        while any(not future.done() for future in futures):
            page = service.store.work_item_events(since=cursor, limit=2)
            observed.extend(page["events"])
            cursor = page["next_cursor"]
        for future in futures:
            future.result()
        while True:
            page = service.store.work_item_events(since=cursor, limit=2)
            observed.extend(page["events"])
            if not page["events"]:
                break
            cursor = page["next_cursor"]

    ids = [event["event_id"] for event in observed]
    assert len(ids) == len(set(ids)) == 13
    assert all(
        event["v"] == 1 and event["type"] == "work_item.event" for event in observed
    )
    assert [event["next"] for event in observed[1:]]


def test_worker_in_flight_cap_is_enforced(service, monkeypatch):
    item = _item(service)
    for number in range(5):
        service.store.create_work_item_subscription(
            f"credential-{number}",
            url="http://127.0.0.1:1/events",
            event_kinds=["updated"],
        )
    service.store.patch_hub_item(item["id"], next_action="wait for CI")
    current = peak = 0
    lock = threading.Lock()

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(*args, **kwargs):
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        threading.Event().wait(0.03)
        with lock:
            current -= 1
        return Response()

    monkeypatch.setattr(
        "drover.server.work_queue.work_item_events._open_delivery", fake_urlopen
    )
    worker = WorkItemEventWorker(
        service.store,
        service.config.work_item_events.model_copy(
            update={"max_in_flight": 2, "batch_size": 5}
        ),
    )
    assert worker.process_once() == 5
    assert peak <= 2


def test_subscription_http_scope_and_secret_once(transport, receiver):
    service, auth, url, device, _, _ = transport
    headers = {"Authorization": "Bearer " + device}
    created = requests.post(
        url + "/work-items/subscriptions",
        json={"url": receiver, "event_kinds": ["worker_done"]},
        headers=headers,
        timeout=5,
    )
    assert created.status_code == 200
    body = created.json()
    assert body["secret"]
    assert (
        "secret"
        not in requests.get(
            url + "/work-items/subscriptions", headers=headers, timeout=5
        ).json()["subscriptions"][0]
    )
    _, other = auth.credentials.issue(scope="device", label="other device")
    other_headers = {"Authorization": "Bearer " + other}
    assert requests.get(
        url + "/work-items/subscriptions", headers=other_headers, timeout=5
    ).json() == {"subscriptions": []}
    assert (
        requests.delete(
            url + "/work-items/subscriptions/" + body["id"],
            headers=other_headers,
            timeout=5,
        ).status_code
        == 404
    )
    assert (
        requests.post(
            url + "/work-items/subscriptions",
            json={"url": "http://example.invalid/events", "event_kinds": []},
            headers=headers,
            timeout=5,
        ).status_code
        == 400
    )
