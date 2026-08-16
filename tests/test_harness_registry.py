"""Tests for the Meta Harness registry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import duckdb
import pytest

import drover.server.harness.registry as registry_module
from drover.schema import bootstrap
from drover.server.db import control_plane_path
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.schema import (
    bootstrap_harness_tables,
    migrate_legacy_harness_event_sequences,
)


def _registry(tmp_path):
    """Return the registry and the path its tables actually live at.

    Since #95 that is the control-plane store, not the lakehouse: the registry
    resolves it from the lakehouse path it is handed, and the resolution is
    idempotent, so ``HarnessRegistry`` accepts either.
    """
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return HarnessRegistry(duckdb_path), control_plane_path(duckdb_path)


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


def test_bootstrap_adds_recap_reconcile_marker_to_existing_sessions(tmp_path):
    """Removing the additive marker migration breaks existing Drover databases."""
    duckdb_path = tmp_path / "pre-marker.duckdb"
    with duckdb.connect(str(duckdb_path)) as con:
        bootstrap_harness_tables(con)
        columns = {
            row[1]
            for row in con.execute("PRAGMA table_info('harness_sessions')").fetchall()
        }
        if "recap_reconcile_needed" in columns:
            con.execute(
                "ALTER TABLE harness_sessions DROP COLUMN recap_reconcile_needed"
            )
        con.execute("""INSERT INTO harness_sessions
                 (session_id, host_id, harness, command, status)
               VALUES ('existing-session', 'nas', 'codex', 'codex', 'running')""")

        bootstrap_harness_tables(con)

        columns = {
            row[1]
            for row in con.execute("PRAGMA table_info('harness_sessions')").fetchall()
        }
        assert "recap_reconcile_needed" in columns
        assert con.execute(
            "SELECT recap_reconcile_needed FROM harness_sessions "
            "WHERE session_id = 'existing-session'"
        ).fetchone() == (False,)


def _model_catalog(
    host_id: str,
    harness: str,
    scope_id: str,
    *,
    discovered_at: str = "2026-08-14T12:00:00+00:00",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "host_id": host_id,
        "harness": harness,
        "account_scope_id": scope_id,
        "harness_version": "1.2.3",
        "discovered_at": discovered_at,
        "stale": False,
        "stale_reason": None,
        "models": [
            {
                "id": f"{harness}-default",
                "display_name": f"{harness} default",
                "description": None,
                "is_default": True,
                "reasoning": None,
            }
        ],
    }


def test_bootstrap_adds_model_catalog_column_without_losing_hosts(tmp_path):
    duckdb_path = tmp_path / "pre-model-catalog.duckdb"
    with duckdb.connect(str(duckdb_path)) as con:
        bootstrap_harness_tables(con)
        con.execute("ALTER TABLE harness_hosts DROP COLUMN model_catalogs_json")
        con.execute("""INSERT INTO harness_hosts
                 (host_id, display_name, kind, status, capabilities_json)
               VALUES ('existing-host', 'Existing', 'macos', 'online', '{}')""")

        bootstrap_harness_tables(con)

        assert con.execute(
            "SELECT host_id, model_catalogs_json FROM harness_hosts"
        ).fetchall() == [("existing-host", "{}")]


def test_model_catalog_cache_is_scoped_and_keeps_latest_per_harness(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.register_host(host_id="mac-mini", display_name="Mac", kind="macos")
    codex_a = _model_catalog("mac-mini", "codex", "scope-a")
    codex_b = _model_catalog(
        "mac-mini",
        "codex",
        "scope-b",
        discovered_at="2026-08-14T12:01:00+00:00",
    )
    codex_c = _model_catalog(
        "mac-mini",
        "codex",
        "scope-c",
        discovered_at="2026-08-14T12:02:00+00:00",
    )
    claude = _model_catalog("mac-mini", "claude-code", "scope-claude")

    registry.save_model_catalog("mac-mini", "codex", "scope-a", codex_a)
    registry.save_model_catalog("mac-mini", "codex", "scope-b", codex_b)
    registry.save_model_catalog("mac-mini", "claude-code", "scope-claude", claude)
    registry.save_model_catalog("mac-mini", "codex", "scope-c", codex_c)

    codex_latest = registry.latest_model_catalog("mac-mini", "codex")
    assert codex_latest == codex_c
    assert registry.latest_model_catalog("mac-mini", "claude-code") == claude
    assert codex_latest is not codex_c
    assert codex_latest is not None
    codex_latest["host_id"] = "mutated"
    assert registry.latest_model_catalog("mac-mini", "codex") == codex_c
    with registry._connect() as con:
        stored = json.loads(
            con.execute(
                "SELECT model_catalogs_json FROM harness_hosts WHERE host_id = ?",
                ["mac-mini"],
            ).fetchone()[0]
        )
    assert list(stored["codex"]["scopes"]) == ["scope-b", "scope-c"]
    assert stored["codex"]["latest_scope_id"] == "scope-c"


def test_model_catalog_cache_rejects_invalid_or_unbounded_writes(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.register_host(host_id="mac-mini", display_name="Mac", kind="macos")
    valid = _model_catalog("mac-mini", "codex", "scope-a")

    with pytest.raises(ValueError, match="unknown harness host"):
        registry.save_model_catalog("missing", "codex", "scope-a", valid)
    with pytest.raises(ValueError, match="account scope"):
        registry.save_model_catalog("mac-mini", "codex", "scope-b", valid)
    with pytest.raises(ValueError, match="non-stale"):
        registry.save_model_catalog(
            "mac-mini",
            "codex",
            "scope-a",
            {**valid, "stale": True, "stale_reason": "offline"},
        )

    oversized = _model_catalog("mac-mini", "codex", "scope-large")
    oversized["models"] = [
        {
            "id": f"model-{index}",
            "display_name": f"Model {index}",
            "description": "x" * 2_048,
            "is_default": index == 0,
            "reasoning": None,
        }
        for index in range(256)
    ]
    with pytest.raises(ValueError, match="public 256 KiB wire limit"):
        registry.save_model_catalog("mac-mini", "codex", "scope-large", oversized)
    assert registry.latest_model_catalog("missing", "codex") is None
    assert registry.latest_model_catalog("mac-mini", "codex") is None


def test_latest_model_catalog_treats_malformed_storage_as_a_cache_miss(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.register_host(host_id="mac-mini", display_name="Mac", kind="macos")
    with registry._connect() as con:
        con.execute(
            "UPDATE harness_hosts SET model_catalogs_json = ? WHERE host_id = ?",
            ["not-json", "mac-mini"],
        )

    assert registry.latest_model_catalog("mac-mini", "codex") is None


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


def test_latest_session_previews_falls_back_to_payload_text(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.create_session(
        session_id="harness-session-preview",
        host_id="mac-mini",
        harness="codex",
        command="codex",
    )
    registry.append_event(
        session_id="harness-session-preview",
        event_type="user_input",
        payload={"text": "Rework session cards for iPhone 17 Pro"},
        content_preview="",
    )

    previews = registry.latest_session_previews(["harness-session-preview"])

    assert previews == {
        "harness-session-preview": "Rework session cards for iPhone 17 Pro"
    }


def test_latest_session_previews_redacts_payload_fallback(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.create_session(
        session_id="harness-session-secret",
        host_id="mac-mini",
        harness="codex",
        command="codex",
    )
    registry.append_event(
        session_id="harness-session-secret",
        event_type="user_input",
        payload={
            "text": "curl -H 'Authorization: Bearer sk-secret' "
            "https://example.test?api_key=sk-query"
        },
        content_preview="",
    )

    preview = registry.latest_session_previews(["harness-session-secret"])[
        "harness-session-secret"
    ]

    assert "sk-secret" not in preview
    assert "sk-query" not in preview
    assert "<redacted>" in preview


def test_latest_session_previews_skips_traceback_payload_fallback(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.create_session(
        session_id="harness-session-traceback",
        host_id="mac-mini",
        harness="codex",
        command="codex",
    )
    registry.append_event(
        session_id="harness-session-traceback",
        event_type="user_input",
        content_preview="Summarize readable session cards",
        seq=1,
    )
    registry.append_event(
        session_id="harness-session-traceback",
        event_type="assistant_output",
        payload={"text": "Traceback (most recent call last):\napi_key=sk-secret"},
        content_preview="",
        seq=2,
    )

    previews = registry.latest_session_previews(["harness-session-traceback"])

    assert previews == {"harness-session-traceback": "Summarize readable session cards"}


def test_latest_session_previews_prefer_task_prompt_over_newer_assistant_text(
    tmp_path,
):
    registry, _ = _registry(tmp_path)
    registry.create_session(
        session_id="harness-session-task-title",
        host_id="mac-mini",
        harness="codex",
        command="codex",
    )
    registry.append_event(
        session_id="harness-session-task-title",
        event_type="user_input",
        content_preview="Fix the session screen sorting and titles",
        seq=1,
    )
    registry.append_event(
        session_id="harness-session-task-title",
        event_type="assistant_output",
        content_preview="I am checking the registry query now.",
        seq=2,
    )

    previews = registry.latest_session_previews(["harness-session-task-title"])

    assert previews == {
        "harness-session-task-title": "Fix the session screen sorting and titles"
    }


def test_append_events_in_order(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.register_host(
        host_id="gpu-pc",
        display_name="GPU PC",
        kind="linux",
        capabilities={"harnesses": ["shell", "agy"]},
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


def test_update_session_native_id_persists_provider_identity(tmp_path):
    registry, _ = _registry(tmp_path)
    session = registry.create_session(
        host_id="h1", harness="codex", command="codex", mode="structured"
    )

    registry.update_session_native_id(session.session_id, "provider-thread-1")

    updated = registry.get_session(session.session_id)
    assert updated is not None
    assert updated.native_session_id == "provider-thread-1"


def test_default_mode_is_pty(tmp_path):
    registry, _ = _registry(tmp_path)
    session = registry.create_session(host_id="h1", harness="shell", command="/bin/sh")
    assert session.mode == "pty"
    assert session.last_activity is None


def test_create_session_defaults_started_at(tmp_path):
    registry, _ = _registry(tmp_path)
    session = registry.create_session(host_id="h1", harness="codex", command="codex")
    assert session.started_at is not None


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


def _seed_event_page(registry: HarnessRegistry, session_id: str, count: int) -> None:
    registry.create_session(
        session_id=session_id,
        host_id="h1",
        harness="claude-code",
        command="claude",
    )
    for seq in range(1, count + 1):
        registry.append_event(
            session_id=session_id,
            event_type="assistant_output",
            payload={"text": f"message {seq}"},
            seq=seq,
        )


def test_event_page_reads_forward_with_fixed_snapshot_bound(tmp_path):
    registry, _ = _registry(tmp_path)
    _seed_event_page(registry, "paged-forward", 7)

    first = registry.list_event_page(
        "paged-forward", after_seq=0, through_seq=5, limit=2
    )
    second = registry.list_event_page(
        "paged-forward", after_seq=2, through_seq=5, limit=2
    )
    final = registry.list_event_page(
        "paged-forward", after_seq=4, through_seq=5, limit=2
    )

    assert [event.seq for event in first.events] == [1, 2]
    assert (first.page_min_seq, first.page_max_seq, first.max_seq) == (1, 2, 5)
    assert first.has_older is False
    assert first.has_newer is True
    assert [event.seq for event in second.events] == [3, 4]
    assert second.has_older is True
    assert second.has_newer is True
    assert [event.seq for event in final.events] == [5]
    assert final.has_newer is False


def test_event_page_reads_newest_tail_and_older_pages(tmp_path):
    registry, _ = _registry(tmp_path)
    _seed_event_page(registry, "paged-backward", 7)

    tail = registry.list_event_page("paged-backward", limit=3)
    older = registry.list_event_page("paged-backward", before_seq=5, limit=2)
    beginning = registry.list_event_page("paged-backward", before_seq=1, limit=2)

    assert [event.seq for event in tail.events] == [5, 6, 7]
    assert (tail.page_min_seq, tail.page_max_seq, tail.max_seq) == (5, 7, 7)
    assert tail.has_older is True
    assert tail.has_newer is False
    assert [event.seq for event in older.events] == [3, 4]
    assert older.has_older is True
    assert older.has_newer is True
    assert beginning.events == []
    assert beginning.page_min_seq is None
    assert beginning.page_max_seq is None
    assert beginning.max_seq == 7
    assert beginning.has_older is False
    assert beginning.has_newer is True


def test_event_page_empty_session_has_empty_metadata(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.create_session(
        session_id="paged-empty", host_id="h1", harness="codex", command="codex"
    )

    page = registry.list_event_page("paged-empty", limit=200)

    assert page.events == []
    assert page.page_min_seq is None
    assert page.page_max_seq is None
    assert page.max_seq == 0
    assert page.has_older is False
    assert page.has_newer is False


def test_event_page_ignores_concurrent_append_after_fixed_bound(tmp_path):
    registry, _ = _registry(tmp_path)
    _seed_event_page(registry, "paged-race", 7)
    first = registry.list_event_page("paged-race", after_seq=0, through_seq=7, limit=4)
    registry.append_event(
        session_id="paged-race",
        event_type="assistant_output",
        payload={"text": "message 8"},
        seq=8,
    )

    second = registry.list_event_page(
        "paged-race", after_seq=4, through_seq=first.max_seq, limit=4
    )

    assert [event.seq for event in second.events] == [5, 6, 7]
    assert second.max_seq == 7
    assert second.has_newer is False


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


def _structured_registry(tmp_path, *, harness="codex"):
    registry, duckdb_path = _registry(tmp_path)
    registry.create_session(
        session_id="s1",
        host_id="laptop",
        harness=harness,
        command=harness,
        mode="structured",
    )
    return registry, duckdb_path


def _recap_job(duckdb_path, session_id):
    with duckdb.connect(str(duckdb_path)) as con:
        return con.execute(
            "SELECT desired_source_seq FROM live_recap_jobs WHERE session_id = ?",
            [session_id],
        ).fetchone()


def test_structured_turn_complete_enqueues_live_recap(tmp_path):
    """A missing structured-completion enqueue would leave live recaps stale."""
    registry, duckdb_path = _structured_registry(tmp_path)

    registry.append_event(
        session_id="s1",
        event_type="status",
        payload={"turn_complete": True},
        seq=12,
    )

    assert _recap_job(duckdb_path, "s1") == (12,)


def test_structured_completion_insert_rolls_back_when_enqueue_fails(
    tmp_path, monkeypatch
):
    """A queue failure must not persist an unqueued completion event."""
    registry, duckdb_path = _structured_registry(tmp_path)

    def unavailable(*_args):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(registry_module, "enqueue_live_recap", unavailable)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        registry.append_event(
            session_id="s1",
            event_type="status",
            payload={"turn_complete": True},
            seq=12,
        )

    assert registry.list_events("s1") == []
    assert _recap_job(duckdb_path, "s1") is None


def test_terminal_and_noncompletion_events_do_not_enqueue_live_recap(tmp_path):
    """Terminal activity and partial structured output are not recap boundaries."""
    terminal, terminal_db = _session_for_events(tmp_path / "terminal")
    terminal.append_event(
        session_id="s1",
        event_type="status",
        payload={"turn_complete": True},
        seq=12,
    )
    structured, structured_db = _structured_registry(tmp_path / "structured")
    structured.append_event(
        session_id="s1",
        event_type="assistant_output",
        payload={"text": "Still working."},
        seq=12,
    )

    assert _recap_job(terminal_db, "s1") is None
    assert _recap_job(structured_db, "s1") is None


def test_batch_mirror_preserves_sequence_and_enqueues_live_recap(tmp_path):
    """The mirrored wire envelope must enqueue at the preserved host sequence."""
    registry, duckdb_path = _structured_registry(tmp_path)

    inserted = registry.append_events_if_new(
        [
            {
                "event_id": "e12",
                "session_id": "s1",
                "event_type": "status",
                "payload": {
                    "event_id": "e12",
                    "session_id": "s1",
                    "seq": 12,
                    "type": "status",
                    "payload": {"turn_complete": True},
                },
                "seq": 12,
            }
        ]
    )

    assert inserted == 1
    assert registry.get_event("e12").seq == 12
    assert _recap_job(duckdb_path, "s1") == (12,)


def test_session_materialization_survives_when_orphan_recap_enqueue_fails(
    tmp_path, monkeypatch
):
    """A derived recap queue failure must not roll back the core session row."""
    registry, duckdb_path = _registry(tmp_path)
    registry.append_event(
        event_id="e12",
        session_id="s1",
        event_type="status",
        payload={"type": "status", "payload": {"turn_complete": True}},
        seq=12,
    )

    def unavailable(*_args):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(registry_module, "enqueue_live_recap", unavailable)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        registry.create_session(
            session_id="s1",
            host_id="laptop",
            harness="codex",
            command="codex",
            mode="structured",
        )

    assert registry.get_session("s1") is not None
    assert registry.get_event("e12") is not None
    assert _recap_job(duckdb_path, "s1") is None


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


def test_bootstrap_drops_legacy_transcript_chunk_table(tmp_path):
    """The chunk table duplicated terminal.output events; bootstrap removes it."""
    from drover.server.harness.schema import bootstrap_harness_tables

    _, duckdb_path = _registry(tmp_path)
    with duckdb.connect(str(duckdb_path)) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS harness_transcript_chunks ("
            "chunk_id VARCHAR PRIMARY KEY, session_id VARCHAR)"
        )

    with duckdb.connect(str(duckdb_path)) as con:
        bootstrap_harness_tables(con)
        tables = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }

    assert "harness_transcript_chunks" not in tables
    assert {"harness_hosts", "harness_sessions", "harness_events"}.issubset(tables)


def test_bootstrap_sequences_all_null_legacy_events_deterministically(tmp_path):
    _, duckdb_path = _registry(tmp_path)
    with duckdb.connect(str(duckdb_path)) as con:
        con.execute("UPDATE harness_events SET seq=NULL")
        con.executemany(
            "INSERT INTO harness_events(event_id,session_id,event_type,payload_json,created_at,seq) VALUES (?,?,?,?,?,NULL)",
            [
                (
                    "event-b",
                    "legacy",
                    "assistant_output",
                    '{"text":"unchanged-b"}',
                    "2026-06-01 10:00:00",
                ),
                (
                    "event-a",
                    "legacy",
                    "user_input",
                    '{"text":"unchanged-a"}',
                    "2026-06-01 10:00:00",
                ),
            ],
        )
        report = migrate_legacy_harness_event_sequences(con)
        rows = con.execute(
            "SELECT event_id,seq,payload_json FROM harness_events "
            "WHERE session_id='legacy' ORDER BY seq"
        ).fetchall()

    assert rows == [
        ("event-a", 1, '{"text":"unchanged-a"}'),
        ("event-b", 2, '{"text":"unchanged-b"}'),
    ]
    assert report.migrated_sessions == 1
    assert report.migrated_events == 2


def test_migration_refuses_mixed_sequence_session_without_mutation(tmp_path):
    _, duckdb_path = _registry(tmp_path)
    with duckdb.connect(str(duckdb_path)) as con:
        con.executemany(
            "INSERT INTO harness_events(event_id,session_id,event_type,payload_json,created_at,seq) VALUES (?,?,?,?,?,?)",
            [
                ("event-1", "mixed", "user_input", "{}", "2026-06-01", 1),
                ("event-2", "mixed", "assistant_output", "{}", "2026-06-02", None),
            ],
        )
        report = migrate_legacy_harness_event_sequences(con)
        rows = con.execute(
            "SELECT event_id,seq FROM harness_events "
            "WHERE session_id='mixed' ORDER BY event_id"
        ).fetchall()

    assert report.mixed_sessions == ("mixed",)
    assert report.migrated_sessions == 0
    assert report.migrated_events == 0
    assert rows == [("event-1", 1), ("event-2", None)]


def test_migration_is_idempotent(tmp_path):
    _, duckdb_path = _registry(tmp_path)
    with duckdb.connect(str(duckdb_path)) as con:
        con.execute(
            "INSERT INTO harness_events(event_id,session_id,event_type,payload_json,created_at,seq) "
            "VALUES ('event-1','legacy','user_input','{}','2026-06-01',NULL)"
        )
        first = migrate_legacy_harness_event_sequences(con)
        second = migrate_legacy_harness_event_sequences(con)
        rows = con.execute(
            "SELECT event_id,seq FROM harness_events WHERE session_id='legacy'"
        ).fetchall()

    assert first.migrated_sessions == 1
    assert first.migrated_events == 1
    assert second.migrated_sessions == 0
    assert second.migrated_events == 0
    assert second.mixed_sessions == ()
    assert rows == [("event-1", 1)]


def _session(reg, host_id, *, status, minutes_ago):
    """Create a session and force its status and recency."""
    s = reg.create_session(host_id=host_id, harness="claude-code", command="x")
    when = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc) - timedelta(
        minutes=minutes_ago
    )
    # `control_plane_path`, not `duckdb_path`: the registry's tables live in
    # the control-plane store since #95.
    with duckdb.connect(str(reg.control_plane_path)) as con:
        con.execute(
            "UPDATE harness_sessions SET status = ?, updated_at = ? WHERE session_id = ?",
            [status, when, s.session_id],
        )
    return s.session_id


def test_list_sessions_caps_archived_but_never_hides_a_live_one(tmp_path):
    """The fleet payload grew without bound: 115 of 120 sessions were terminated.

    Capping archived sessions keeps the poll payload small, but a live session
    must never be dropped to make room -- being unable to see a running
    session is a far worse failure than a long list.
    """
    reg, _ = _registry(tmp_path)
    reg.register_host(host_id="h1", display_name="H1", kind="macos")

    archived = [
        _session(reg, "h1", status="terminated", minutes_ago=i) for i in range(10)
    ]
    live = [
        _session(reg, "h1", status="running", minutes_ago=500),
        _session(reg, "h1", status="created", minutes_ago=600),
    ]

    got = reg.list_sessions(archived_limit=3)
    ids = [s.session_id for s in got]

    # Every live session survives, however stale it looks.
    for sid in live:
        assert sid in ids, "a live session was dropped by the archived cap"

    kept_archived = [sid for sid in ids if sid in archived]
    assert len(kept_archived) == 3
    # The three most recently updated archived sessions, not an arbitrary three.
    assert kept_archived == archived[:3]


def test_list_sessions_without_a_limit_returns_everything(tmp_path):
    reg, _ = _registry(tmp_path)
    reg.register_host(host_id="h1", display_name="H1", kind="macos")
    for i in range(5):
        _session(reg, "h1", status="terminated", minutes_ago=i)

    assert len(reg.list_sessions()) == 5
    assert len(reg.list_sessions(archived_limit=None)) == 5


def test_list_sessions_treats_an_unknown_status_as_live(tmp_path):
    """An unrecognised status must not be capped away.

    Statuses are written in several places; if a new one appears, hiding it
    silently would make a real session invisible. Unknown means live here.
    """
    reg, _ = _registry(tmp_path)
    reg.register_host(host_id="h1", display_name="H1", kind="macos")
    for i in range(4):
        _session(reg, "h1", status="terminated", minutes_ago=i)
    odd = _session(reg, "h1", status="quiesced", minutes_ago=999)

    ids = [s.session_id for s in reg.list_sessions(archived_limit=1)]
    assert odd in ids


# -- a mirrored event arriving twice ---------------------------------------
#
# The host daemon retains undelivered event batches and re-offers them (#101),
# so an event whose delivery succeeded but whose acknowledgement was lost
# arrives again carrying the same event_id. The hub's mirror path guards with
# `if registry.get_event(event_id) is not None: continue`, which is a
# check-then-act: two deliveries in flight together both find nothing and both
# insert. That produced 195 `Duplicate key "event_id: ..."` tracebacks in one
# server log, each one an event the hub already had.


def test_appending_a_mirrored_event_twice_is_a_no_op(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.create_session(
        session_id="harness-session-mirror",
        host_id="mac-mini",
        harness="agy",
        command="agy",
    )
    first = registry.append_event(
        session_id="harness-session-mirror",
        event_type="assistant_output",
        payload={"text": "hello"},
        event_id="harness-event-fixed",
        seq=1,
    )

    second = registry.append_event(
        session_id="harness-session-mirror",
        event_type="assistant_output",
        payload={"text": "hello"},
        event_id="harness-event-fixed",
        seq=1,
    )

    assert second is not None
    assert second.event_id == first.event_id
    events = registry.list_events("harness-session-mirror")
    assert len(events) == 1, f"one event, redelivered; got {len(events)}"


def test_a_redelivered_event_does_not_overwrite_what_was_stored(tmp_path):
    """Re-delivery is a no-op, not a last-writer-wins update.

    The retained copy on the host is the same event, but nothing guarantees a
    replay carries identical derived fields, and the hub's copy is the one the
    app has already read.
    """

    registry, _ = _registry(tmp_path)
    registry.create_session(
        session_id="harness-session-mirror-2",
        host_id="mac-mini",
        harness="agy",
        command="agy",
    )
    registry.append_event(
        session_id="harness-session-mirror-2",
        event_type="assistant_output",
        payload={"text": "original"},
        event_id="harness-event-stable",
        seq=1,
    )

    registry.append_event(
        session_id="harness-session-mirror-2",
        event_type="assistant_output",
        payload={"text": "replayed"},
        event_id="harness-event-stable",
        seq=1,
    )

    stored = registry.get_event("harness-event-stable")
    assert stored.payload["text"] == "original"


def test_ingest_structured_events_mixed_aware_and_naive_timestamps(tmp_path):
    registry, _ = _registry(tmp_path)
    registry.register_host(host_id="mac-mini", display_name="Mac mini", kind="macos")
    session = registry.create_session(
        session_id="session-tz-mix",
        host_id="mac-mini",
        harness="claude-code",
        command="claude",
        mode="structured",
    )

    t0_naive = datetime(2026, 8, 16, 10, 0, 0)
    # Aware +02:00 at 12:30 is 10:30 UTC
    t1_aware_non_utc = datetime(
        2026, 8, 16, 12, 30, 0, tzinfo=timezone(timedelta(hours=2))
    )
    # Aware UTC at 11:00 UTC
    t2_aware_utc = datetime(2026, 8, 16, 11, 0, 0, tzinfo=timezone.utc)

    # Ingest a batch containing mixed naive and aware timestamps in the SAME call
    count = registry.ingest_structured_events(
        [
            {
                "event_id": "ev-1",
                "session_id": session.session_id,
                "event_type": "user_input",
                "payload": {"text": "hello"},
                "seq": 1,
                "created_at": t0_naive,
            },
            {
                "event_id": "ev-2",
                "session_id": session.session_id,
                "event_type": "assistant_output",
                "payload": {"text": "mid response"},
                "seq": 2,
                "created_at": t1_aware_non_utc,
            },
            {
                "event_id": "ev-3",
                "session_id": session.session_id,
                "event_type": "status",
                "payload": {"turn_complete": True},
                "seq": 3,
                "created_at": t2_aware_utc,
            },
        ]
    )
    assert count == 3

    # Check that session projection updated properly
    s = registry.get_session(session.session_id)
    assert s is not None
    # last_activity should resolve to the correct UTC instant of t2 (11:00:00 UTC)
    assert registry_module._db_timestamp_to_utc(s.last_activity) == t2_aware_utc

    # Replay all events - must be idempotent and not fail
    replay_count = registry.ingest_structured_events(
        [
            {
                "event_id": "ev-1",
                "session_id": session.session_id,
                "event_type": "user_input",
                "payload": {"text": "hello"},
                "seq": 1,
                "created_at": t0_naive,
            },
            {
                "event_id": "ev-2",
                "session_id": session.session_id,
                "event_type": "assistant_output",
                "payload": {"text": "mid response"},
                "seq": 2,
                "created_at": t1_aware_non_utc,
            },
            {
                "event_id": "ev-3",
                "session_id": session.session_id,
                "event_type": "status",
                "payload": {"turn_complete": True},
                "seq": 3,
                "created_at": t2_aware_utc,
            },
        ]
    )
    assert replay_count == 0

    # Also test an interior gap rebuild scenario: an event arriving with seq <= existing max
    # triggers rebuild querying DuckDB (which yields naive timestamps) mixed with incoming event
    count3 = registry.ingest_structured_events(
        [
            {
                "event_id": "ev-rebuild",
                "session_id": session.session_id,
                "event_type": "status",
                "payload": {"awaiting": "input"},
                "seq": 2,
                "created_at": datetime(2026, 8, 16, 10, 45, 0, tzinfo=timezone.utc),
            }
        ]
    )
    assert count3 == 1
    s_rebuild = registry.get_session(session.session_id)
    assert s_rebuild is not None
    assert registry_module._db_timestamp_to_utc(s_rebuild.last_activity) == t2_aware_utc
