"""Tests for structured event reconciliation on restart and reconnect."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic, sleep

import pytest

from drover.schema import bootstrap
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    create_harness_server,
    register_daemon_host,
    wire_event_pusher,
)
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.structured.pusher import EventPusher, reconcile_unsent_events
from drover.server.metrics import MetricsCollector, start_metrics_server
from drover.server.web.auth import AuthSettings


class _FakeCentralHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    status_sequence: list[int] = []
    failing: bool = False

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


def _wait_until(predicate, *, timeout: float = 5.0) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(0.02)
    return predicate()


def _init_registry(tmp_path, name: str = "test") -> HarnessRegistry:
    db_path = tmp_path / f"{name}.duckdb"
    bootstrap(parquet_dir=tmp_path / f"{name}-parquet", duckdb_path=db_path)
    return HarnessRegistry(db_path)


def test_reconcile_unsent_events_pushes_stored_events_to_central(tmp_path):
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        registry = _init_registry(tmp_path)
        session = registry.create_session(
            host_id="host-1",
            harness="claude-code",
            command="claude",
            mode="structured",
        )
        registry.append_event(
            session_id=session.session_id,
            event_type="user_input",
            payload={"text": "hello", "type": "user_input"},
            seq=1,
            normalized_source="structured",
        )
        registry.append_event(
            session_id=session.session_id,
            event_type="assistant_output",
            payload={"text": "hi there", "type": "assistant_output"},
            seq=2,
            normalized_source="structured",
        )

        pusher = EventPusher(f"http://127.0.0.1:{port}", "secret-token")
        count = reconcile_unsent_events(registry, pusher)
        assert count == 2
        assert len(_FakeCentralHandler.requests) == 1
        req = _FakeCentralHandler.requests[0]
        assert req["path"] == "/harness/events"
        assert req["authorization"] == "Bearer secret-token"
        events = req["body"]["events"]
        assert len(events) == 2
        assert events[0]["seq"] == 1
        assert events[0]["type"] == "user_input"
        assert events[1]["seq"] == 2
        assert events[1]["type"] == "assistant_output"
    finally:
        server.shutdown()
        server.server_close()


def test_reconcile_unsent_events_handles_empty_db(tmp_path):
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        registry = _init_registry(tmp_path)
        pusher = EventPusher(f"http://127.0.0.1:{port}", "secret-token")
        count = reconcile_unsent_events(registry, pusher)
        assert count == 0
        assert len(_FakeCentralHandler.requests) == 0
    finally:
        server.shutdown()
        server.server_close()


def test_reconcile_unsent_events_unconfigured_pusher(tmp_path):
    registry = _init_registry(tmp_path)
    session = registry.create_session(
        host_id="host-1",
        harness="claude-code",
        command="claude",
        mode="structured",
    )
    registry.append_event(
        session_id=session.session_id,
        event_type="user_input",
        payload={"text": "hello", "type": "user_input"},
        seq=1,
        normalized_source="structured",
    )

    pusher = EventPusher("", "")
    assert not pusher.is_configured()
    assert reconcile_unsent_events(registry, pusher) == 0
    assert reconcile_unsent_events(registry, None) == 0


def test_reconcile_unsent_events_buffers_when_central_unavailable(tmp_path):
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        _FakeCentralHandler.failing = True

        registry = _init_registry(tmp_path)
        session = registry.create_session(
            host_id="host-1",
            harness="claude-code",
            command="claude",
            mode="structured",
        )
        registry.append_event(
            session_id=session.session_id,
            event_type="user_input",
            payload={"text": "hello", "type": "user_input"},
            seq=1,
            normalized_source="structured",
        )

        pusher = EventPusher(
            f"http://127.0.0.1:{port}", "secret-token", batch_interval=0.1
        )
        pusher.start()

        # Reconcile fails immediate POST and buffers into pusher
        reconciled = reconcile_unsent_events(registry, pusher)
        assert reconciled == 1
        assert len(pusher._unsent) == 1

        # Central recovers
        _FakeCentralHandler.failing = False
        assert _wait_until(
            lambda: any(
                req.get("body", {}).get("events")
                and req["body"]["events"][0]["type"] == "user_input"
                for req in _FakeCentralHandler.requests
            ),
            timeout=5.0,
        )
        pusher.stop()
    finally:
        server.shutdown()
        server.server_close()


def test_reconcile_helpers_on_registry_and_pusher(tmp_path):
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        registry = _init_registry(tmp_path)
        session = registry.create_session(
            host_id="host-1",
            harness="claude-code",
            command="claude",
            mode="structured",
        )
        registry.append_event(
            session_id=session.session_id,
            event_type="user_input",
            payload={"text": "via pusher.reconcile", "type": "user_input"},
            seq=1,
            normalized_source="structured",
        )

        pusher = EventPusher(f"http://127.0.0.1:{port}", "secret-token")
        assert pusher.reconcile(registry) == 1
        assert registry.reconcile_unsent_events(pusher) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_list_recent_events_filtering(tmp_path):
    registry = _init_registry(tmp_path)
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(hours=48)
    recent_time = now - timedelta(hours=2)

    s1 = registry.create_session(
        host_id="host-a",
        harness="claude-code",
        command="claude",
        mode="structured",
    )
    s2 = registry.create_session(
        host_id="host-b",
        harness="codex",
        command="codex",
        mode="structured",
    )

    # Old event (> 24h)
    registry.append_event(
        session_id=s1.session_id,
        event_type="user_input",
        payload={"text": "old"},
        seq=1,
        created_at=old_time,
        normalized_source="structured",
    )
    # Recent event on host-a
    registry.append_event(
        session_id=s1.session_id,
        event_type="assistant_output",
        payload={"text": "recent a"},
        seq=2,
        created_at=recent_time,
        normalized_source="structured",
    )
    # Recent event on host-b
    registry.append_event(
        session_id=s2.session_id,
        event_type="assistant_output",
        payload={"text": "recent b"},
        seq=1,
        created_at=recent_time,
        normalized_source="structured",
    )

    # Default 24h query
    events_all = registry.list_recent_events_for_reconciliation(since_hours=24)
    assert len(events_all) == 2
    assert {e.session_id for e in events_all} == {s1.session_id, s2.session_id}

    # Host filtered
    events_host_a = registry.list_recent_events_for_reconciliation(
        since_hours=24, host_id="host-a"
    )
    assert len(events_host_a) == 1
    assert events_host_a[0].session_id == s1.session_id

    # Larger window includes the 48h old event
    events_older = registry.list_recent_events_for_reconciliation(
        since_hours=72, host_id="host-a"
    )
    assert len(events_older) == 2


def test_e2e_restart_event_reconciliation(tmp_path):
    """End-to-end: Pre-populated local DuckDB events reconcile to central on daemon boot."""
    import urllib.request

    test_token = "e2e-token"
    auth = AuthSettings(enabled=True, api_token=test_token)
    auth_headers = {"Authorization": f"Bearer {test_token}"}

    # 1. Start central server
    central_db = tmp_path / "central.duckdb"
    bootstrap(parquet_dir=tmp_path / "central-parquet", duckdb_path=central_db)
    collector = MetricsCollector(
        duckdb_path=central_db,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    central_server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=auth
    )
    central_port = central_server.server_address[1]
    central_url = f"http://127.0.0.1:{central_port}"

    # 2. Seed daemon DuckDB with an orphaned session and events (as if it died before push)
    daemon_db = tmp_path / "daemon.duckdb"
    bootstrap(parquet_dir=tmp_path / "daemon-parquet", duckdb_path=daemon_db)
    daemon_registry = HarnessRegistry(daemon_db)
    session = daemon_registry.create_session(
        host_id="test-daemon",
        harness="claude-code",
        command="claude",
        mode="structured",
        status="running",
    )
    daemon_registry.append_event(
        session_id=session.session_id,
        event_type="user_input",
        payload={"text": "persisted user turn", "type": "user_input"},
        seq=1,
        normalized_source="structured",
    )
    daemon_registry.append_event(
        session_id=session.session_id,
        event_type="assistant_output",
        payload={"text": "persisted assistant answer", "type": "assistant_output"},
        seq=2,
        normalized_source="structured",
    )
    daemon_registry.append_event(
        session_id=session.session_id,
        event_type="status",
        payload={"turn_complete": True, "type": "status"},
        seq=3,
        normalized_source="structured",
    )

    # 3. Boot daemon: wire_event_pusher & create_harness_server run reconciliation
    state = HarnessDaemonState(
        host_id="test-daemon",
        display_name="Test Daemon",
        kind="linux",
        registry=daemon_registry,
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        local_url="http://127.0.0.1:0",
        api_token=test_token,
        central_url=central_url,
    )
    register_daemon_host(state)
    pusher = wire_event_pusher(state)
    daemon_server = create_harness_server(
        listen_host="127.0.0.1", listen_port=0, state=state
    )
    daemon_thread = threading.Thread(target=daemon_server.serve_forever, daemon=True)
    daemon_thread.start()

    try:
        # 4. Verify central received all 3 events that were in local DuckDB
        def _get_messages():
            req = urllib.request.Request(
                f"{central_url}/harness/sessions/{session.session_id}/messages",
                headers=auth_headers,
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return json.loads(resp.read().decode("utf-8"))["messages"]

        assert _wait_until(
            lambda: len(_get_messages()) == 3,
            timeout=10.0,
        )

        messages = _get_messages()
        assert len(messages) == 3
        assert [m["seq"] for m in messages] == [1, 2, 3]
        assert [m["type"] for m in messages] == [
            "user_input",
            "assistant_output",
            "status",
        ]
        assert messages[1]["text"] == "persisted assistant answer"

        # 5. Verify local session was marked errored (orphaned on restart)
        local_session = daemon_registry.get_session(session.session_id)
        assert local_session.status == "errored"
    finally:
        if pusher is not None:
            pusher.stop()
        daemon_server.shutdown()
        daemon_server.server_close()
        central_server.shutdown()
