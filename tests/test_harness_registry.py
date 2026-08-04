"""Tests for the Meta Harness registry."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb

from drover.schema import bootstrap
from drover.server.harness.registry import HarnessRegistry


def _registry(tmp_path):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return HarnessRegistry(duckdb_path), duckdb_path


def test_bootstrap_creates_harness_tables(tmp_path):
    _, duckdb_path = _registry(tmp_path)
    with duckdb.connect(str(duckdb_path)) as con:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_type = 'BASE TABLE'"
            ).fetchall()
        }

    assert {
        "harness_hosts",
        "harness_sessions",
        "harness_events",
    }.issubset(tables)


def test_register_host_upserts_capabilities_and_heartbeat(tmp_path):
    registry, _ = _registry(tmp_path)

    first = registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        local_url="http://192.168.1.70:7081",
        capabilities={"harnesses": ["shell"]},
    )
    second = registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        local_url="http://192.168.1.70:7081",
        tailscale_url="http://100.64.0.10:7081",
        capabilities={"harnesses": ["shell", "codex"]},
    )

    hosts = registry.list_hosts()
    assert len(hosts) == 1
    assert first.host_id == second.host_id == "nas"
    assert second.status == "online"
    assert second.tailscale_url == "http://100.64.0.10:7081"
    assert second.capabilities == {"harnesses": ["shell", "codex"]}
    assert second.last_seen_at is not None


def test_register_host_persists_connection_kind(tmp_path):
    registry, _ = _registry(tmp_path)
    host = registry.register_host(
        host_id="work-laptop",
        display_name="Work Laptop",
        kind="mac",
        connection_kind="relay",
    )
    assert host.connection_kind == "relay"
    fetched = registry.get_host("work-laptop")
    assert fetched is not None and fetched.connection_kind == "relay"


def test_register_host_defaults_connection_kind_direct(tmp_path):
    registry, _ = _registry(tmp_path)
    host = registry.register_host(host_id="mini", display_name="Mac Mini", kind="mac")
    assert host.connection_kind == "direct"


def test_create_and_update_session_lifecycle(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        capabilities={"harnesses": ["shell", "claude-code"]},
    )

    session = registry.create_session(
        session_id="harness-session-1",
        host_id="mac-mini",
        harness="shell",
        command="/bin/zsh",
        repo_owner="arniesaha",
        repo_name="nexus",
        branch="main",
        cwd="/Users/arnabmac/jenny/nexus",
        status="running",
        started_at=datetime(2026, 6, 21, 22, tzinfo=timezone.utc),
    )
    updated = registry.update_session_status(
        session.session_id,
        "ended",
        ended_at=datetime(2026, 6, 21, 23, tzinfo=timezone.utc),
        summary_session_id="summary-1",
    )

    assert updated.status == "ended"
    assert updated.summary_session_id == "summary-1"
    assert registry.get_session("harness-session-1") == updated
    assert registry.list_sessions(host_id="mac-mini", status="ended") == [updated]
    assert registry.list_sessions(status="running") == []


def test_append_events_in_order(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.register_host(
        host_id="gpu-pc",
        display_name="GPU PC",
        kind="linux",
        capabilities={"harnesses": ["shell", "gemini"]},
    )
    registry.create_session(
        session_id="harness-session-2",
        host_id="gpu-pc",
        harness="shell",
        command="/bin/bash",
    )

    event = registry.append_event(
        session_id="harness-session-2",
        event_type="session.started",
        payload={"pid": 1234},
    )
    for seq, text in enumerate(["first", "second", "third"], start=1):
        registry.append_event(
            session_id="harness-session-2",
            event_type="terminal.output",
            payload={"text": text},
            seq=seq,
        )

    assert registry.list_events("harness-session-2")[0].payload == {"pid": 1234}
    assert event.event_type == "session.started"
    assert event.normalized_type == "status"
    assert event.normalized_source == "structured"
    assert event.content_preview == "session started"
    outputs = [
        e.payload["text"]
        for e in registry.list_events("harness-session-2")
        if e.event_type == "terminal.output"
    ]
    assert outputs == ["first", "second", "third"]


def test_structured_session_fields_roundtrip(tmp_path):
    registry, _ = _registry(tmp_path)
    session = registry.create_session(
        host_id="h1", harness="claude-code", command="claude -p", mode="structured"
    )
    assert session.mode == "structured"
    assert session.awaiting is None
    registry.update_session_activity(session.session_id, awaiting="approval")
    updated = registry.get_session(session.session_id)
    assert updated.awaiting == "approval"
    assert updated.last_activity is not None
    registry.update_session_activity(session.session_id, awaiting=None)
    assert registry.get_session(session.session_id).awaiting is None


def test_default_mode_is_pty(tmp_path):
    registry, _ = _registry(tmp_path)
    session = registry.create_session(host_id="h1", harness="shell", command="/bin/sh")
    assert session.mode == "pty"
    assert session.last_activity is None


def test_event_seq_ordering(tmp_path):
    registry, _ = _registry(tmp_path)
    session = registry.create_session(host_id="h1", harness="claude-code", command="c")
    sid = session.session_id
    assert registry.max_event_seq(sid) == 0
    for seq in (1, 2, 3):
        registry.append_event(
            session_id=sid,
            event_type="assistant_output",
            payload={"seq": seq},
            seq=seq,
        )
    assert registry.max_event_seq(sid) == 3
    tail = registry.list_events_after(sid, 1)
    assert [event.seq for event in tail] == [2, 3]
    # events without seq (PTY mirror path) are excluded from seq listings
    registry.append_event(session_id=sid, event_type="terminal.output", payload={})
    assert [e.seq for e in registry.list_events_after(sid, 0)] == [1, 2, 3]


def test_append_event_stores_queryable_normalized_terminal_metadata(tmp_path):
    registry, duckdb_path = _registry(tmp_path)
    registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        capabilities={"harnesses": ["shell"]},
    )
    registry.create_session(
        session_id="harness-session-normalized",
        host_id="nas",
        harness="shell",
        command="/bin/sh",
    )

    event = registry.append_event(
        session_id="harness-session-normalized",
        event_type="terminal.input",
        harness="shell",
        payload={"text": "ls -la\n", "byte_count": 7},
    )

    assert event.normalized_type == "command"
    assert event.normalized_source == "inferred_terminal"
    assert event.content_preview == "ls -la"
    with duckdb.connect(str(duckdb_path)) as con:
        rows = con.execute(
            """
            SELECT event_type, normalized_type, normalized_source, content_preview
            FROM harness_events
            WHERE session_id = ?
            """,
            ["harness-session-normalized"],
        ).fetchall()
    assert rows == [("terminal.input", "command", "inferred_terminal", "ls -la")]


def test_concurrent_writes_across_registry_instances_are_serialized(tmp_path):
    """Regression test: DuckDB raises "Unique file handle conflict" when two
    threads call duckdb.connect() on the same file at nearly the same
    instant. HarnessRegistry._connect() must serialize the whole
    connect->use->close window per resolved db path -- across INSTANCES too,
    since central builds a fresh HarnessRegistry per request. Before the
    fix, this test failed intermittently with BinderException.
    """
    import threading

    _, duckdb_path = _registry(tmp_path)
    # Two separate instances pointing at the same file, exercised from many
    # threads: per-instance locking would not be enough here.
    registries = [HarnessRegistry(duckdb_path), HarnessRegistry(duckdb_path)]
    for index, registry in enumerate(registries):
        registry.create_session(
            session_id=f"harness-conc-{index}",
            host_id="nas",
            harness="shell",
            command="/bin/sh",
        )

    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def hammer(registry: HarnessRegistry, session_id: str) -> None:
        try:
            barrier.wait(timeout=5)
            for seq in range(1, 26):
                registry.append_event(
                    session_id=session_id,
                    event_type="assistant_output",
                    payload={"text": f"{session_id}-{seq}"},
                    seq=seq,
                )
                registry.update_session_activity(session_id, awaiting=None)
        except Exception as exc:  # noqa: BLE001 - surfaced via errors list
            errors.append(exc)

    threads = [
        threading.Thread(
            target=hammer, args=(registries[i % 2], f"harness-conc-{i % 2}")
        )
        for i in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    for index in range(2):
        events = registries[index].list_events_after(f"harness-conc-{index}", 0)
        # Two threads per session each wrote seqs 1..25; the second thread's
        # duplicate-seq rows still land (no unique constraint) -- the point
        # here is purely that no connect ever raised.
        assert len(events) == 50


def _session_for_events(tmp_path):
    registry, duckdb_path = _registry(tmp_path)
    registry.register_host(host_id="laptop", display_name="Laptop", kind="mac")
    registry.create_session(
        session_id="s1",
        host_id="laptop",
        harness="shell",
        command="/bin/sh",
        status="running",
    )
    return registry, duckdb_path


def _record(event_id, **overrides):
    record = {
        "event_id": event_id,
        "session_id": "s1",
        "event_type": "terminal.output",
        "payload": {"data": event_id},
        "normalized_type": None,
        "normalized_source": None,
        "content_preview": None,
        "created_at": None,
    }
    record.update(overrides)
    return record


def test_append_events_if_new_writes_a_batch_in_one_window(tmp_path):
    """The terminal mirror's write path: many events, one connection."""
    registry, _ = _session_for_events(tmp_path)
    records = [_record(f"evt-{index}") for index in range(20)]

    connects = {"count": 0}
    real_connect = HarnessRegistry._connect

    def counting_connect(self):
        connects["count"] += 1
        return real_connect(self)

    HarnessRegistry._connect = counting_connect
    try:
        written = registry.append_events_if_new(records)
    finally:
        HarnessRegistry._connect = real_connect

    assert written == 20
    assert connects["count"] == 1, "batched write still opened one connection each"
    assert len(registry.list_events("s1")) == 20


def test_append_events_if_new_skips_ids_already_stored(tmp_path):
    """Idempotency by event_id is what makes replaying a stream safe."""
    registry, _ = _session_for_events(tmp_path)
    assert registry.append_events_if_new([_record("evt-a"), _record("evt-b")]) == 2
    # Same batch again, plus one genuinely new event.
    written = registry.append_events_if_new(
        [_record("evt-a"), _record("evt-b"), _record("evt-c")]
    )
    assert written == 1
    assert {event.event_id for event in registry.list_events("s1")} == {
        "evt-a",
        "evt-b",
        "evt-c",
    }


def test_append_events_if_new_dedupes_within_one_batch(tmp_path):
    """A burst can carry the same event twice; the insert must not."""
    registry, _ = _session_for_events(tmp_path)
    written = registry.append_events_if_new(
        [_record("evt-dup"), _record("evt-dup"), _record("evt-other")]
    )
    assert written == 2
    assert len(registry.list_events("s1")) == 2


def test_append_events_if_new_ignores_records_without_an_id(tmp_path):
    registry, _ = _session_for_events(tmp_path)
    assert registry.append_events_if_new([]) == 0
    assert registry.append_events_if_new([_record(""), _record("  ")]) == 0
    assert registry.list_events("s1") == []


def test_create_session_persists_permission_mode(tmp_path):
    registry, _ = _registry(tmp_path)
    session = registry.create_session(
        host_id="h1",
        harness="claude-code",
        command="claude",
        mode="structured",
        permission_mode="auto",
    )
    fetched = registry.get_session(session.session_id)
    assert fetched is not None
    assert fetched.permission_mode == "auto"


def test_create_session_permission_mode_defaults_to_none(tmp_path):
    registry, _ = _registry(tmp_path)
    session = registry.create_session(host_id="h1", harness="shell", command="sh")
    fetched = registry.get_session(session.session_id)
    assert fetched.permission_mode is None


def test_transcript_text_rebuilds_structured_session_from_events(tmp_path):
    """Structured sessions never write transcript chunks.

    Reading chunks alone handed every structured handoff an empty transcript,
    so the continuation started with no conversation context at all.
    """
    registry, _ = _registry(tmp_path)
    session = registry.create_session(
        host_id="h1", harness="claude-code", command="claude", mode="structured"
    )
    for seq, (event_type, text) in enumerate(
        [
            ("user_input", "add retries"),
            ("assistant_output", "on it"),
            ("tool_action", "Edit(main.py)"),
            ("tool_result", "1 file changed"),
        ],
        start=1,
    ):
        registry.append_event(
            session_id=session.session_id,
            event_type=event_type,
            payload={"text": text},
            seq=seq,
        )

    transcript = registry.transcript_text(session.session_id)
    assert transcript.splitlines() == [
        "[user] add retries",
        "[assistant] on it",
        "[tool] Edit(main.py)",
        "[tool-result] 1 file changed",
    ]


def test_transcript_text_is_empty_when_session_has_no_content(tmp_path):
    registry, _ = _registry(tmp_path)
    session = registry.create_session(host_id="h1", harness="shell", command="sh")
    registry.append_event(
        session_id=session.session_id, event_type="session.started", payload={}
    )

    assert registry.transcript_text(session.session_id) == ""
