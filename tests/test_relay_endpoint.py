"""GET /harness/relay: upgrades a spoke's websocket and hands it to RelayManager."""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass

import pytest

from drover.schema import bootstrap
from drover.server.harness.relay_protocol import hello_frame, req_frame, res_frame
from drover.server.harness.websocket import (
    OPCODE_PING,
    WebSocketClosed,
    client_handshake,
    client_recv_json,
    client_send_frame,
    client_send_json,
)
from drover.server.metrics import MetricsCollector
from drover.server.web import app as app_module
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings
from drover.server.web.credentials import CredentialStore


@dataclass
class _MetricsServer:
    host: str
    port: int
    token: str
    collector: MetricsCollector
    server: object


@pytest.fixture
def metrics_server(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
    )
    store = CredentialStore(tmp_path / "credentials.json")
    _, token = store.issue(scope="host", label="host-a", host_id="host-a")
    collector.api_token = token
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(enabled=True, api_token="operator", credentials=store),
    )
    host, port = server.server_address
    try:
        yield _MetricsServer(
            host=host, port=port, token=token, collector=collector, server=server
        )
    finally:
        server.shutdown()
        server.server_close()


def _connect_and_hello(
    metrics_server, host_id: str, *, capabilities: list[str] | None = None
) -> socket.socket:
    host, port, token = metrics_server.host, metrics_server.port, metrics_server.token
    _, token = metrics_server.server.RequestHandlerClass.auth.credentials.issue(
        scope="host", label=host_id, host_id=host_id
    )
    sock = socket.create_connection((host, port), timeout=5)
    client_handshake(
        sock,
        host=f"{host}:{port}",
        path="/harness/relay",
        headers={"Authorization": f"Bearer {token}", "X-Drover-Host-ID": host_id},
    )
    client_send_json(sock, hello_frame(host_id, capabilities=capabilities))
    return sock


def _spoke_recv(sock: socket.socket) -> dict:
    """Read frames, skipping pong/ping Nones, like test_relay_manager.py does."""
    while True:
        frame = client_recv_json(sock)
        if frame is not None:
            return frame


def test_relay_upgrade_registers_live_host(metrics_server):
    sock = _connect_and_hello(metrics_server, "work-laptop")
    # attach is async from the client's perspective; poll briefly
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if metrics_server.collector.relay_manager.is_live("work-laptop"):
            break
        time.sleep(0.05)
    assert metrics_server.collector.relay_manager.is_live("work-laptop")

    # is_live() only proves a flag flipped. Prove the hijacked fd actually
    # survived the handler's finish()/shutdown_request() cleanup and still
    # works bidirectionally by driving one real req/res round-trip through
    # RelayManager.request(), with this test playing the spoke -- mirrors
    # test_relay_manager.py's test_request_round_trip.
    result: dict[str, object] = {}

    def _issue_request() -> None:
        status, body = metrics_server.collector.relay_manager.request(
            "work-laptop", "GET", "/sessions", None, timeout_s=5
        )
        result["status"] = status
        result["body"] = body

    thread = threading.Thread(target=_issue_request, daemon=True)
    thread.start()
    try:
        frame = _spoke_recv(sock)
        assert frame["kind"] == "req"
        assert frame["method"] == "GET"
        assert frame["path"] == "/sessions"
        client_send_json(sock, res_frame(frame["id"], 200, '{"sessions": []}\n'))
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert result["status"] == 200
        assert json.loads(result["body"]) == {"sessions": []}
    finally:
        sock.close()


def test_relay_upgrade_negotiates_framed_response_capability(metrics_server):
    sock = _connect_and_hello(
        metrics_server,
        "framed-laptop",
        capabilities=["framed_responses_v1"],
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if metrics_server.collector.relay_manager.is_live("framed-laptop"):
            break
        time.sleep(0.05)

    result: dict[str, object] = {}

    def issue_bounded_request() -> None:
        result["value"] = metrics_server.collector.relay_manager.request(
            "framed-laptop",
            "POST",
            "/advisory/content-bundle",
            {"target_ids": ["global-agents"]},
            timeout_s=5,
            max_response_bytes=4096,
        )

    thread = threading.Thread(target=issue_bounded_request, daemon=True)
    thread.start()
    try:
        frame = _spoke_recv(sock)
        assert frame["response_framing"] == "framed_responses_v1"
        client_send_json(
            sock,
            {
                "kind": "res_start",
                "id": frame["id"],
                "status": 200,
                "body_bytes": 2,
            },
        )
        client_send_frame(sock, 0x1, b"{}")
        thread.join(timeout=5)
        assert result["value"] == (200, "{}")
    finally:
        sock.close()


def test_relay_upgrade_requires_token(metrics_server):
    host, port = metrics_server.host, metrics_server.port
    sock = socket.create_connection((host, port), timeout=5)
    try:
        with pytest.raises(RuntimeError):  # handshake sees non-101 status
            client_handshake(sock, host=f"{host}:{port}", path="/harness/relay")
    finally:
        sock.close()


def test_relay_upgrade_rejects_wrong_kind_frame(metrics_server):
    host, port, token = metrics_server.host, metrics_server.port, metrics_server.token
    sock = socket.create_connection((host, port), timeout=5)
    try:
        client_handshake(
            sock,
            host=f"{host}:{port}",
            path="/harness/relay",
            headers={"Authorization": f"Bearer {token}"},
        )
        # A well-formed but non-hello frame must be rejected: the endpoint
        # only ever accepts a hello as the first frame.
        client_send_json(sock, req_frame("req-1", "GET", "/sessions", None))
        sock.settimeout(5)
        with pytest.raises(WebSocketClosed):
            _spoke_recv(sock)
        assert metrics_server.collector.relay_manager.live_host_ids() == set()
    finally:
        sock.close()


def test_relay_upgrade_rejects_empty_host_id(metrics_server):
    host, port, token = metrics_server.host, metrics_server.port, metrics_server.token
    sock = socket.create_connection((host, port), timeout=5)
    try:
        client_handshake(
            sock,
            host=f"{host}:{port}",
            path="/harness/relay",
            headers={"Authorization": f"Bearer {token}"},
        )
        client_send_json(sock, hello_frame(""))
        sock.settimeout(5)
        with pytest.raises(WebSocketClosed):
            _spoke_recv(sock)
        assert metrics_server.collector.relay_manager.live_host_ids() == set()
    finally:
        sock.close()


def test_hello_budget_is_total_not_per_recv(metrics_server, monkeypatch):
    """A pinger must not hold a handler thread open indefinitely.

    settimeout() is per socket operation, and recv_json answers a ping and
    returns None -- so a client pinging faster than the timeout used to reset
    the clock forever. On ThreadingHTTPServer that is one pinned thread per
    connection with no cap, reachable from the internet by anyone holding the
    shared token once the funnel is up.
    """
    monkeypatch.setattr(app_module, "RELAY_HELLO_TIMEOUT_S", 1.0)
    host, port, token = metrics_server.host, metrics_server.port, metrics_server.token
    sock = socket.create_connection((host, port), timeout=10)
    try:
        client_handshake(
            sock,
            host=f"{host}:{port}",
            path="/harness/relay",
            headers={"Authorization": f"Bearer {token}"},
        )
        sock.settimeout(20)
        started = time.monotonic()
        # Ping steadily and never say hello. Under the per-recv bug this loop
        # runs until the test's own deadline; under a total budget the server
        # gives up and closes.
        with pytest.raises((WebSocketClosed, OSError)):
            while time.monotonic() - started < 15:
                client_send_frame(sock, OPCODE_PING, b"stall")
                time.sleep(0.2)
                # Drains the pong, and raises once the server hangs up.
                client_recv_json(sock)
        elapsed = time.monotonic() - started
        assert elapsed < 10, f"handler stayed pinned for {elapsed:.1f}s"
        assert metrics_server.collector.relay_manager.live_host_ids() == set()
    finally:
        sock.close()


def test_relay_bound_credential_cannot_declare_another_host(metrics_server, caplog):
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"http://{metrics_server.host}:{metrics_server.port}/harness/relay",
        headers={
            "Authorization": f"Bearer {metrics_server.token}",
            "X-Drover-Host-ID": "host-b",
            "Upgrade": "websocket",
            "Sec-WebSocket-Key": "test",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request)
    assert exc.value.code == 403
    assert "host credential identity mismatch" in caplog.text
    assert not metrics_server.collector.relay_manager.is_live("host-b")


def test_relay_hello_cannot_override_binding(metrics_server, caplog):
    sock = socket.create_connection(
        (metrics_server.host, metrics_server.port), timeout=5
    )
    try:
        client_handshake(
            sock,
            host=f"{metrics_server.host}:{metrics_server.port}",
            path="/harness/relay",
            headers={"Authorization": f"Bearer {metrics_server.token}"},
        )
        client_send_json(sock, hello_frame("host-b"))
        with pytest.raises(WebSocketClosed):
            _spoke_recv(sock)
        assert not metrics_server.collector.relay_manager.is_live("host-b")
        assert "relay hello host credential identity mismatch" in caplog.text
    finally:
        sock.close()


def test_rotation_invalidates_attached_relay(metrics_server):
    sock = _connect_and_hello(metrics_server, "host-a")
    try:
        deadline = time.monotonic() + 5
        while (
            not metrics_server.collector.relay_manager.is_live("host-a")
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert metrics_server.collector.relay_manager.is_live("host-a")
        metrics_server.server.RequestHandlerClass.auth.credentials.issue(
            scope="host", label="host-a", host_id="host-a"
        )
        assert not metrics_server.collector.relay_manager.is_live("host-a")
    finally:
        sock.close()


def _post(metrics_server, path, payload, token=None):
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"http://{metrics_server.host}:{metrics_server.port}" + path,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token or metrics_server.token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _post_json(metrics_server, path, payload):
    import urllib.request

    request = urllib.request.Request(
        f"http://{metrics_server.host}:{metrics_server.port}" + path,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {metrics_server.token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read())


@pytest.mark.parametrize("path", ["/harness/hosts", "/harness/hosts/host-b/heartbeat"])
def test_host_cannot_register_or_heartbeat_another_host(metrics_server, path, caplog):
    assert _post(metrics_server, path, {"host_id": "host-b"}) == 403
    assert "host credential identity mismatch" in caplog.text


def test_matching_registration_and_revocation(metrics_server):
    assert _post(metrics_server, "/harness/hosts", {"host_id": "host-a"}) == 200
    assert _post(metrics_server, "/harness/hosts/host-a/heartbeat", {}) == 200
    store = metrics_server.server.RequestHandlerClass.auth.credentials
    store.revoke(store.find_active(metrics_server.token).id)
    assert _post(metrics_server, "/harness/hosts", {"host_id": "host-a"}) == 401


def test_event_batch_rejects_cross_host_sessions_before_writing(metrics_server):
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(metrics_server.collector.duckdb_path)
    for host_id in ("host-a", "host-b"):
        registry.register_host(host_id=host_id, display_name=host_id, kind="linux")
        registry.create_session(
            session_id=host_id + "-session",
            host_id=host_id,
            harness="claude",
            command="synthetic",
            cwd="/synthetic",
        )
    events = [
        {
            "event_id": host + "-event",
            "session_id": host + "-session",
            "type": "text",
            "text": "synthetic",
        }
        for host in ("host-a", "host-b")
    ]
    assert _post(metrics_server, "/harness/events", {"events": events}) == 403
    assert registry.list_events_after("host-a-session", 0) == []
    assert _post(metrics_server, "/harness/events", {"events": events[:1]}) == 200
    assert (
        _post(
            metrics_server,
            "/harness/events",
            {"events": [dict(events[0], host_id="host-b")]},
        )
        == 403
    )


def test_shared_operator_token_cannot_ingest_as_host(metrics_server):
    assert (
        _post(metrics_server, "/harness/hosts", {"host_id": "host-a"}, token="operator")
        == 403
    )


def test_heartbeat_body_cannot_override_credential(metrics_server):
    assert (
        _post(metrics_server, "/harness/hosts/host-a/heartbeat", {"host_id": "host-b"})
        == 403
    )


def test_revoked_host_cannot_upgrade_relay(metrics_server):
    import urllib.error
    import urllib.request

    store = metrics_server.server.RequestHandlerClass.auth.credentials
    store.revoke(store.find_active(metrics_server.token).id)
    request = urllib.request.Request(
        f"http://{metrics_server.host}:{metrics_server.port}/harness/relay",
        headers={
            "Authorization": f"Bearer {metrics_server.token}",
            "X-Drover-Host-ID": "host-a",
            "Upgrade": "websocket",
            "Sec-WebSocket-Key": "test",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request)
    assert exc.value.code == 401


def test_event_ingress_requires_host_credential_even_for_empty_batch(metrics_server):
    assert (
        _post(metrics_server, "/harness/events", {"events": []}, token="operator")
        == 403
    )
    assert _post(metrics_server, "/harness/events", {"events": []}) == 200


def test_unknown_session_is_skipped_only_after_observed_retry_window(
    metrics_server, monkeypatch
):
    from drover.server.harness.registry import HarnessRegistry

    now = [100.0]
    monkeypatch.setattr(
        app_module, "_ownership_retry_clock", lambda: now[0], raising=False
    )
    registry = HarnessRegistry(metrics_server.collector.duckdb_path)
    batch = {
        "events": [
            {
                "event_id": "orphan-event",
                "session_id": "absent-session",
                "type": "status",
                "ts": "2020-01-01T00:00:00+00:00",
            }
        ]
    }
    # Event age cannot establish an orphan; the hub must observe failed retries.
    assert _post(metrics_server, "/harness/events", batch) == 403
    now[0] += 24 * 60 * 60 - 1
    assert _post(metrics_server, "/harness/events", batch) == 403
    now[0] += 1
    # A fresh request-handler class simulates a hub process restart.  It must
    # recover the original observation time from the control-plane store.
    metrics_server.server.RequestHandlerClass.unknown_session_retries = (
        app_module._UnknownSessionRetries(registry)
    )
    status, response = _post_json(metrics_server, "/harness/events", batch)
    assert status == 200
    assert response["orphaned"] == ["absent-session"]
    assert registry.list_events_after("absent-session", 0) == []


def test_unknown_session_retry_window_survives_hub_restart(metrics_server, monkeypatch):
    from drover.server.harness.registry import HarnessRegistry

    now = [100.0]
    monkeypatch.setattr(app_module, "_ownership_retry_clock", lambda: now[0])
    registry = HarnessRegistry(metrics_server.collector.duckdb_path)
    first_hub = app_module._UnknownSessionRetries(registry)
    assert not first_hub.expired("host-a", "missing")
    now[0] += 24 * 60 * 60
    restarted_hub = app_module._UnknownSessionRetries(registry)
    assert restarted_hub.expired("host-a", "missing")


def test_reconciliation_acks_pruned_duplicates_and_marks_local_ledger(
    metrics_server, tmp_path, caplog
):
    from datetime import datetime, timezone

    from drover.schema import bootstrap
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.harness.structured.pusher import EventPusher

    central = HarnessRegistry(metrics_server.collector.duckdb_path)
    central.ingest_structured_events(
        [
            {
                "event_id": "pruned-duplicate",
                "session_id": "pruned-session",
                "event_type": "assistant_output",
                "payload": {"event_id": "pruned-duplicate", "type": "assistant_output"},
                "seq": 1,
                "created_at": datetime.now(timezone.utc),
            }
        ]
    )
    local_path = tmp_path / "local.duckdb"
    bootstrap(parquet_dir=tmp_path / "local-parquet", duckdb_path=local_path)
    local = HarnessRegistry(local_path)
    local.create_session(
        session_id="pruned-session",
        host_id="host-a",
        harness="claude",
        command="synthetic",
        mode="structured",
    )
    local.append_event(
        session_id="pruned-session",
        event_type="assistant_output",
        event_id="pruned-duplicate",
        seq=1,
        payload={"event_id": "pruned-duplicate", "type": "assistant_output"},
        normalized_source="structured",
    )
    pusher = EventPusher(
        f"http://{metrics_server.host}:{metrics_server.port}", metrics_server.token
    )

    assert pusher.reconcile(local, host_id="host-a") == 1
    assert local.list_events_for_reconciliation(host_id="host-a") == []
    assert "ownership backoff" not in caplog.text


def test_rejection_log_identifies_route_and_session_owner(metrics_server, caplog):
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(metrics_server.collector.duckdb_path)
    registry.register_host(host_id="host-b", display_name="B", kind="linux")
    registry.create_session(
        session_id="other-session",
        host_id="host-b",
        harness="claude",
        command="synthetic",
    )
    assert (
        _post(
            metrics_server,
            "/harness/events",
            {
                "events": [
                    {
                        "event_id": "other-event",
                        "session_id": "other-session",
                        "type": "status",
                    }
                ]
            },
        )
        == 403
    )
    assert "route=/harness/events" in caplog.text
    assert "session_id=other-session" in caplog.text
    assert "expected_host_id=host-b" in caplog.text
    assert "credential_host_id=host-a" in caplog.text
    assert metrics_server.token not in caplog.text


def test_expired_orphan_does_not_block_known_owner_in_same_batch(
    metrics_server, monkeypatch
):
    from drover.server.harness.registry import HarnessRegistry

    now = [100.0]
    monkeypatch.setattr(
        app_module, "_ownership_retry_clock", lambda: now[0], raising=False
    )
    registry = HarnessRegistry(metrics_server.collector.duckdb_path)
    registry.register_host(host_id="host-a", display_name="A", kind="linux")
    registry.create_session(
        session_id="known", host_id="host-a", harness="claude", command="synthetic"
    )
    events = [
        {"event_id": "old-event", "session_id": "absent", "type": "status", "seq": 1},
        {
            "event_id": "new-event",
            "session_id": "known",
            "type": "assistant_output",
            "seq": 1,
            "text": "Hello",
        },
    ]
    events.insert(
        1,
        {
            "event_id": "another-old-event",
            "session_id": "also-absent",
            "type": "status",
            "seq": 1,
        },
    )
    assert _post(metrics_server, "/harness/events", {"events": events}) == 200
    assert [e.event_id for e in registry.list_events_after("known", 0)] == ["new-event"]
    now[0] += 24 * 60 * 60
    assert _post(metrics_server, "/harness/events", {"events": events}) == 200
    assert [e.event_id for e in registry.list_events_after("known", 0)] == ["new-event"]
    assert registry.list_events_after("absent", 0) == []
    # Even an unknown session may not claim another host in its envelope.
    assert (
        _post(
            metrics_server,
            "/harness/events",
            {"events": [dict(events[0], host_id="host-b")]},
        )
        == 403
    )


def test_pusher_retains_early_events_until_hub_registers_ownership(
    metrics_server, monkeypatch
):
    from drover.server.harness.daemon import reset_undelivered_event_count
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.harness.structured import pusher as pusher_module
    from drover.server.harness.structured.pusher import EventPusher

    now = [100.0]
    monkeypatch.setattr(pusher_module, "monotonic", lambda: now[0])
    registry = HarnessRegistry(metrics_server.collector.duckdb_path)
    pusher = EventPusher(
        f"http://{metrics_server.host}:{metrics_server.port}", metrics_server.token
    )
    batch = [
        {
            "event_id": "early-event",
            "session_id": "early-session",
            "type": "assistant_output",
            "seq": 1,
            "text": "Hello",
        }
    ]
    reset_undelivered_event_count()
    try:
        pusher._deliver_with_retries(batch)
        retained = pusher._drain()
        assert retained == batch
        assert registry.list_events_after("early-session", 0) == []
        registry.register_host(host_id="host-a", display_name="A", kind="linux")
        registry.create_session(
            session_id="early-session",
            host_id="host-a",
            harness="claude-code",
            command="claude",
        )
        # Registration does not bypass the client's per-session ownership backoff.
        assert not pusher.post_batch(retained)
        now[0] += 30
        pusher._deliver_with_retries(retained)
        assert pusher._drain() == []
        assert [e.event_id for e in registry.list_events_after("early-session", 0)] == [
            "early-event"
        ]
    finally:
        reset_undelivered_event_count()


def test_orphan_window_is_host_scoped_and_eviction_extends_retries(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(app_module, "_ownership_retry_clock", lambda: now[0])
    monkeypatch.setattr(app_module, "_MAX_UNKNOWN_SESSION_RETRIES", 2)
    retries = app_module._UnknownSessionRetries()
    assert not retries.expired("host-a", "s1")
    now[0] += 24 * 60 * 60
    assert retries.expired("host-a", "s1")
    assert not retries.expired("host-b", "s1")
    assert not retries.expired("host-a", "s2")
    # Capacity eviction starts a fresh retry window, never an immediate skip.
    assert not retries.expired("host-a", "s1")
    now[0] += 24 * 60 * 60
    assert retries.expired("host-a", "s1")
    retries.forget("host-a", "s1")
    assert not retries.expired("host-a", "s1")


def test_mixed_batch_retains_only_pending_session_and_delivers_after_registration(
    metrics_server, monkeypatch
):
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.harness.structured import pusher as pusher_module
    from drover.server.harness.structured.pusher import EventPusher

    now = [100.0]
    monkeypatch.setattr(pusher_module, "monotonic", lambda: now[0])
    registry = HarnessRegistry(metrics_server.collector.duckdb_path)
    registry.register_host(host_id="host-a", display_name="A", kind="linux")
    registry.create_session(
        session_id="live", host_id="host-a", harness="claude", command="synthetic"
    )
    pusher = EventPusher(
        f"http://{metrics_server.host}:{metrics_server.port}", metrics_server.token
    )

    def event(session, seq):
        return dict(
            event_id=f"{session}-{seq}",
            session_id=session,
            type="assistant_output",
            seq=seq,
            ts="2026-10-11T00:00:00+00:00",
            text="Hello",
        )

    batch = [
        event("live", 1),
        event("pending", 1),
        event("live", 2),
        event("pending", 2),
    ]
    pusher._deliver_with_retries(batch)
    retained = pusher._drain()
    assert retained == [batch[1], batch[3]]
    assert [e.seq for e in registry.list_events_after("live", 0)] == [1, 2]
    assert registry.list_events_after("pending", 0) == []
    # A new live event goes through even while the pending session backs off.
    pusher._deliver_with_retries(retained + [event("live", 3)])
    retained = pusher._drain()
    assert retained == [batch[1], batch[3]]
    assert [e.seq for e in registry.list_events_after("live", 0)] == [1, 2, 3]
    registry.create_session(
        session_id="pending", host_id="host-a", harness="claude", command="synthetic"
    )
    now[0] += 30
    pusher._deliver_with_retries(retained)
    assert pusher._drain() == []
    assert [e.seq for e in registry.list_events_after("pending", 0)] == [1, 2]
    # Canonical replays with a different event_id are also fenced.
    assert pusher.post_batch(batch + [dict(batch[0], event_id="alternate-id")])
    assert [e.seq for e in registry.list_events_after("live", 0)] == [1, 2, 3]
    assert [e.seq for e in registry.list_events_after("pending", 0)] == [1, 2]


def test_reconciliation_continues_past_pending_session_page(metrics_server, tmp_path):
    from drover.schema import bootstrap
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.harness.structured.pusher import EventPusher

    central = HarnessRegistry(metrics_server.collector.duckdb_path)
    central.register_host(host_id="host-a", display_name="A", kind="linux")
    central.create_session(
        session_id="live", host_id="host-a", harness="claude", command="synthetic"
    )
    local_path = tmp_path / "local.duckdb"
    bootstrap(parquet_dir=tmp_path / "local-parquet", duckdb_path=local_path)
    local = HarnessRegistry(local_path)
    for session_id in ("orphan", "live"):
        local.create_session(
            session_id=session_id,
            host_id="host-a",
            harness="claude",
            command="synthetic",
        )
        local.append_event(
            session_id=session_id,
            event_type="assistant_output",
            seq=1,
            payload={"text": "Hello"},
        )
    pusher = EventPusher(
        f"http://{metrics_server.host}:{metrics_server.port}", metrics_server.token
    )
    assert pusher.reconcile(local, host_id="host-a", batch_size=1) is None
    assert len(central.list_events_after("live", 0)) == 1
    assert central.list_events_after("orphan", 0) == []
    assert pusher._drain() == []  # pending durable pages stay in the ledger
    assert pusher.reconcile(local, host_id="host-a", batch_size=1) is None
    assert len(central.list_events_after("live", 0)) == 1
