"""Tests for structured event reconciliation on restart and reconnect."""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic, sleep

import pytest

from drover.schema import bootstrap
from drover.server.harness import registry as registry_module
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    _heartbeat_once,
    create_harness_server,
    register_daemon_host,
    wire_event_pusher,
)
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.models import HarnessEvent
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


def test_reconcile_unsent_events_retries_from_durable_ledger_after_recovery(tmp_path):
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

        # A failed pass remains durable without entering the bounded queue.
        reconciled = reconcile_unsent_events(registry, pusher)
        assert reconciled is None
        assert pusher._drain() == []

        # A later pass starts from DuckDB again and succeeds.
        _FakeCentralHandler.failing = False
        assert reconcile_unsent_events(registry, pusher) == 1
        delivery_attempts = [
            req
            for req in _FakeCentralHandler.requests
            if req.get("body", {}).get("events")
            and req["body"]["events"][0]["type"] == "user_input"
        ]
        assert len(delivery_attempts) == 2
        pusher.stop()
    finally:
        server.shutdown()
        server.server_close()


def test_failed_reconciliation_stays_in_duckdb_instead_of_the_bounded_retry_buffer(
    tmp_path,
):
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
            payload={"text": "still durable", "type": "user_input"},
            seq=1,
            normalized_source="structured",
        )
        pusher = EventPusher(f"http://127.0.0.1:{port}", "secret-token")

        result = reconcile_unsent_events(registry, pusher)

        assert result is None
        assert pusher._drain() == []
        assert [event.seq for event in registry.list_events(session.session_id)] == [1]
    finally:
        server.shutdown()
        server.server_close()


def test_daemon_retries_durable_reconciliation_after_a_successful_heartbeat(tmp_path):
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
            payload={"text": "retry from DuckDB", "type": "user_input"},
            seq=1,
            normalized_source="structured",
        )
        state = HarnessDaemonState(
            host_id="host-1",
            display_name="Host One",
            kind="mac",
            registry=registry,
            pty=PtySessionManager(),
            presets=DEFAULT_PRESETS,
            local_url="http://127.0.0.1:0",
            api_token="secret-token",
            central_url=f"http://127.0.0.1:{port}",
        )

        pusher = wire_event_pusher(state)
        assert pusher is not None
        try:
            assert _wait_until(
                lambda: any(
                    request["path"] == "/harness/events"
                    for request in _FakeCentralHandler.requests
                )
            )
            first_worker = state.event_reconciliation_thread
            assert first_worker is not None
            first_worker.join(timeout=2.0)
            assert state.event_reconciliation_pending is True
            assert pusher._drain() == []

            _FakeCentralHandler.failing = False
            _heartbeat_once(state)

            assert _wait_until(lambda: state.event_reconciliation_pending is False)
            delivered = [
                request
                for request in _FakeCentralHandler.requests
                if request["path"] == "/harness/events"
                and request["body"].get("events")
            ]
            assert len(delivered) == 2
            assert delivered[-1]["body"]["events"][0]["event_id"] == (
                registry.list_events(session.session_id)[0].event_id
            )
        finally:
            pusher.stop()
    finally:
        server.shutdown()
        server.server_close()


def test_reconciliation_pages_until_every_durable_event_was_offered():
    server = _start_fake_central()
    try:
        port = server.server_address[1]
        created_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
        events = [
            HarnessEvent(
                event_id=f"e{seq}",
                session_id="s1",
                event_type="assistant_output",
                payload={"text": str(seq), "type": "assistant_output"},
                seq=seq,
                created_at=created_at + timedelta(seconds=seq),
            )
            for seq in range(1, 4)
        ]

        class _PagedRegistry:
            def list_events_for_reconciliation(
                self,
                *,
                host_id=None,
                after_created_at=None,
                after_event_id=None,
                limit=100,
            ):
                del host_id
                remaining = events
                if after_created_at is not None:
                    remaining = [
                        event
                        for event in events
                        if (event.created_at, event.event_id)
                        > (after_created_at, after_event_id)
                    ]
                return remaining[:limit]

        pusher = EventPusher(f"http://127.0.0.1:{port}", "secret-token")

        assert reconcile_unsent_events(_PagedRegistry(), pusher, batch_size=2) == 3
        assert [
            event["event_id"]
            for request in _FakeCentralHandler.requests
            for event in request["body"]["events"]
        ] == ["e1", "e2", "e3"]
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


def test_list_events_for_reconciliation_filters_host_and_pages_without_age_cutoff(
    tmp_path,
):
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

    events_all = registry.list_events_for_reconciliation()
    assert len(events_all) == 3
    assert {e.session_id for e in events_all} == {s1.session_id, s2.session_id}

    events_host_a = registry.list_events_for_reconciliation(host_id="host-a")
    assert [event.seq for event in events_host_a] == [1, 2]

    first_page = registry.list_events_for_reconciliation(host_id="host-a", limit=1)
    assert [event.seq for event in first_page] == [1]
    second_page = registry.list_events_for_reconciliation(
        host_id="host-a",
        after_created_at=first_page[-1].created_at,
        after_event_id=first_page[-1].event_id,
        limit=1,
    )
    assert [event.seq for event in second_page] == [2]


def test_default_reconciliation_query_does_not_abandon_old_structured_events(tmp_path):
    registry = _init_registry(tmp_path)
    session = registry.create_session(
        host_id="host-a",
        harness="claude-code",
        command="claude",
        mode="structured",
    )
    registry.append_event(
        session_id=session.session_id,
        event_type="assistant_output",
        payload={"text": "older than the old replay window"},
        seq=1,
        created_at=datetime.now(timezone.utc) - timedelta(days=7),
        normalized_source="structured",
    )

    assert [
        event.seq for event in registry.list_events_for_reconciliation(host_id="host-a")
    ] == [1]


def test_interior_gap_repair_rebuilds_derived_state_from_the_full_sequence(tmp_path):
    token = "reconciliation-token"
    central_db = tmp_path / "central-derived.duckdb"
    bootstrap(parquet_dir=tmp_path / "central-derived-parquet", duckdb_path=central_db)
    collector = MetricsCollector(
        duckdb_path=central_db,
        incoming_dir=tmp_path / "central-derived-incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    registry = HarnessRegistry(central_db)
    registry.create_session(
        host_id="host-a",
        harness="claude-code",
        command="claude",
        session_id="session-with-gap",
        mode="structured",
        status="running",
    )
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(enabled=True, api_token=token),
    )
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    def post(events):
        request = urllib.request.Request(
            f"{base_url}/harness/events",
            data=json.dumps({"events": events}).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            assert response.status == 200

    common = {"session_id": "session-with-gap"}
    try:
        post(
            [
                {
                    **common,
                    "event_id": "event-1",
                    "seq": 1,
                    "type": "user_input",
                    "payload": {},
                    "ts": "2026-08-01T00:00:01+00:00",
                },
                {
                    **common,
                    "event_id": "event-3",
                    "seq": 3,
                    "type": "approval_response",
                    "payload": {"request_id": "request-1", "decision": "allow"},
                    "ts": "2026-08-01T00:00:03+00:00",
                },
                {
                    **common,
                    "event_id": "event-4",
                    "seq": 4,
                    "type": "status",
                    "payload": {"awaiting": "input", "turn_complete": True},
                    "ts": "2026-08-01T00:00:04+00:00",
                },
            ]
        )
        before_repair = registry.get_session("session-with-gap")
        assert before_repair.awaiting == "input"

        post(
            [
                {
                    **common,
                    "event_id": "event-2",
                    "seq": 2,
                    "type": "approval_prompt",
                    "payload": {"request_id": "request-1"},
                    "ts": "2026-08-01T00:00:02+00:00",
                }
            ]
        )

        repaired = registry.get_session("session-with-gap")
        assert repaired.awaiting == "input"
        assert repaired.last_activity == before_repair.last_activity
    finally:
        server.shutdown()


def test_remote_ingest_rolls_back_event_when_projection_derivation_fails(
    tmp_path, monkeypatch
):
    registry = _init_registry(tmp_path)
    session = registry.create_session(
        host_id="host-a",
        harness="claude-code",
        command="claude",
        mode="structured",
        status="running",
    )
    original = registry.get_session(session.session_id)

    def fail_derivation(*_args, **_kwargs):
        raise RuntimeError("projection failed")

    monkeypatch.setattr(
        registry_module, "_derive_structured_awaiting", fail_derivation, raising=False
    )

    with pytest.raises(RuntimeError, match="projection failed"):
        registry.ingest_structured_events(
            [
                {
                    "event_id": "atomic-event",
                    "session_id": session.session_id,
                    "event_type": "approval_prompt",
                    "payload": {
                        "event_id": "atomic-event",
                        "session_id": session.session_id,
                        "seq": 1,
                        "type": "approval_prompt",
                        "payload": {"request_id": "request-1"},
                    },
                    "seq": 1,
                    "created_at": datetime.now(timezone.utc),
                }
            ]
        )

    assert registry.get_event("atomic-event") is None
    repaired = registry.get_session(session.session_id)
    assert repaired.awaiting == original.awaiting
    assert repaired.last_activity == original.last_activity


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


def test_e2e_restart_event_reconciliation_mixed_timestamps(tmp_path):
    """End-to-end: Pre-populated local events with mixed naive and aware timestamps reconcile."""
    test_token = "e2e-token-tz"
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

    # 2. Seed daemon DuckDB with events having naive and aware timestamps
    daemon_db = tmp_path / "daemon.duckdb"
    bootstrap(parquet_dir=tmp_path / "daemon-parquet", duckdb_path=daemon_db)
    daemon_registry = HarnessRegistry(daemon_db)
    session = daemon_registry.create_session(
        host_id="test-daemon-tz",
        harness="claude-code",
        command="claude",
        mode="structured",
        status="running",
    )
    t0_naive = datetime(2026, 8, 16, 10, 0, 0)
    t1_aware_non_utc = datetime(
        2026, 8, 16, 12, 30, 0, tzinfo=timezone(timedelta(hours=2))
    )
    t2_aware_utc = datetime(2026, 8, 16, 11, 0, 0, tzinfo=timezone.utc)

    daemon_registry.append_event(
        session_id=session.session_id,
        event_type="user_input",
        payload={"text": "turn with naive time", "type": "user_input"},
        seq=1,
        created_at=t0_naive,
        normalized_source="structured",
    )
    daemon_registry.append_event(
        session_id=session.session_id,
        event_type="assistant_output",
        payload={"text": "answer with aware non-utc", "type": "assistant_output"},
        seq=2,
        created_at=t1_aware_non_utc,
        normalized_source="structured",
    )
    daemon_registry.append_event(
        session_id=session.session_id,
        event_type="status",
        payload={"turn_complete": True, "type": "status"},
        seq=3,
        created_at=t2_aware_utc,
        normalized_source="structured",
    )

    # 3. Boot daemon: wire_event_pusher & create_harness_server run reconciliation
    state = HarnessDaemonState(
        host_id="test-daemon-tz",
        display_name="Test Daemon TZ",
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
        # 4. Verify central received all 3 events without error
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
        assert messages[0]["text"] == "turn with naive time"
        assert messages[1]["text"] == "answer with aware non-utc"
    finally:
        if pusher is not None:
            pusher.stop()
        daemon_server.shutdown()
        daemon_server.server_close()
        central_server.shutdown()

