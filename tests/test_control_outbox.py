"""PostgreSQL control-event outbox and archive lifecycle tests."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest


@pytest.fixture
def postgres_control_store(tmp_path: Path, monkeypatch):
    dsn = os.environ.get("DROVER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("DROVER_TEST_POSTGRES_DSN is required for PostgreSQL integration")

    from drover.config import ControlStoreConfig
    from drover.server.control_store import close_control_store, configure_control_store
    from drover.schema import bootstrap

    control_path = tmp_path / "control.duckdb"
    config = ControlStoreConfig(
        backend="postgres",
        dsn_env="DROVER_TEST_POSTGRES_DSN",
        pool_min_size=1,
        pool_max_size=2,
        acquire_timeout_seconds=5.0,
        statement_timeout_seconds=5.0,
        schema=f"drover_task2_{uuid4().hex}",
    )
    monkeypatch.setenv(config.dsn_env, dsn)
    configure_control_store(control_path, config)
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=control_path)
    try:
        yield control_path, tmp_path / "parquet"
    finally:
        close_control_store(control_path)
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'DROP SCHEMA IF EXISTS "{config.schema}" CASCADE')


@pytest.fixture
def postgres_single_connection_control_store(tmp_path: Path, monkeypatch):
    """A one-slot store proves archive RPCs cannot hold API connections."""
    dsn = os.environ.get("DROVER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("DROVER_TEST_POSTGRES_DSN is required for PostgreSQL integration")
    from drover.config import ControlStoreConfig
    from drover.server.control_store import close_control_store, configure_control_store
    from drover.schema import bootstrap

    control_path = tmp_path / "single-slot-control.duckdb"
    config = ControlStoreConfig(
        backend="postgres",
        dsn_env="DROVER_TEST_POSTGRES_DSN",
        pool_min_size=1,
        pool_max_size=1,
        acquire_timeout_seconds=0.25,
        statement_timeout_seconds=5.0,
        schema=f"drover_task2_single_{uuid4().hex}",
    )
    monkeypatch.setenv(config.dsn_env, dsn)
    configure_control_store(control_path, config)
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=control_path)
    try:
        yield control_path
    finally:
        close_control_store(control_path)
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'DROP SCHEMA IF EXISTS "{config.schema}" CASCADE')


def test_postgres_event_write_commits_payload_preview_and_outbox_atomically(
    postgres_control_store,
):
    """Dropping any derived control write must roll back the event itself."""
    control_path, _ = postgres_control_store
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="host-1", display_name="Host", kind="test")
    registry.create_session(
        host_id="host-1", harness="codex", command="codex", session_id="session-1"
    )

    registry.append_event(
        session_id="session-1",
        event_id="event-1",
        event_type="user_input",
        payload={"text": "ship the control-store change"},
        content_preview="ship the control-store change",
        seq=7,
    )

    with control_plane_connection(control_path) as con:
        assert con.execute(
            "SELECT payload_json FROM harness_events WHERE event_id = ?", ["event-1"]
        ).fetchone() == (None,)
        assert con.execute(
            "SELECT payload_json FROM harness_event_payloads WHERE event_id = ?",
            ["event-1"],
        ).fetchone() == ('{"text":"ship the control-store change"}',)
        assert con.execute(
            "SELECT event_id FROM harness_session_previews WHERE session_id = ?",
            ["session-1"],
        ).fetchone() == ("event-1",)
        assert con.execute(
            "SELECT state FROM control_outbox_events WHERE event_id = ?", ["event-1"]
        ).fetchone() == ("pending",)


def test_postgres_event_transaction_rolls_back_metadata_payload_preview_outbox_and_recap(
    postgres_control_store, monkeypatch
):
    """A post-recap failure cannot leave a partially durable event behind."""
    control_path, _ = postgres_control_store
    from drover.server.db import control_plane_connection
    from drover.server.harness import registry as registry_module
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="rollback-host", display_name="Host", kind="test")
    registry.create_session(
        host_id="rollback-host",
        harness="codex",
        command="codex",
        session_id="rollback-session",
        mode="structured",
    )
    actual_enqueue = registry_module._enqueue_recap_if_completion

    def crash_after_recap(*args, **kwargs):
        actual_enqueue(*args, **kwargs)
        raise RuntimeError("simulated process failure after recap intent")

    monkeypatch.setattr(
        registry_module, "_enqueue_recap_if_completion", crash_after_recap
    )
    with pytest.raises(RuntimeError, match="simulated process failure"):
        registry.append_event(
            session_id="rollback-session",
            event_id="rollback-event",
            event_type="status",
            payload={"turn_complete": True},
            seq=1,
        )

    with control_plane_connection(control_path) as con:
        for table in (
            "harness_events",
            "harness_event_payloads",
            "harness_session_previews",
            "control_outbox_events",
            "live_recap_jobs",
        ):
            assert con.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_postgres_batch_and_structured_writers_keep_every_new_event_exportable(
    postgres_control_store,
):
    """A writer added later must not create hot rows that the archive misses."""
    control_path, _ = postgres_control_store
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="host-2", display_name="Host", kind="test")
    registry.create_session(
        host_id="host-2", harness="codex", command="codex", session_id="batch-session"
    )
    registry.create_session(
        host_id="host-2",
        harness="codex",
        command="codex",
        session_id="structured-session",
        mode="structured",
    )

    assert (
        registry.append_events_if_new(
            [
                {
                    "event_id": "batch-1",
                    "session_id": "batch-session",
                    "event_type": "assistant_output",
                    "payload": {"text": "first"},
                    "seq": 1,
                },
                {
                    "event_id": "batch-2",
                    "session_id": "batch-session",
                    "event_type": "assistant_output",
                    "payload": {"text": "second"},
                    "seq": 2,
                },
            ]
        )
        == 2
    )
    assert (
        registry.ingest_structured_events(
            [
                {
                    "event_id": "structured-1",
                    "session_id": "structured-session",
                    "event_type": "user_input",
                    "payload": {"payload": {"text": "resume this"}},
                    "seq": 1,
                }
            ]
        )
        == 1
    )

    with control_plane_connection(control_path) as con:
        assert con.execute(
            "SELECT count(*) FROM harness_event_payloads"
        ).fetchone() == (3,)
        assert con.execute(
            "SELECT count(*) FROM control_outbox_events WHERE state = 'pending'"
        ).fetchone() == (3,)

    # A delivery that loses an acknowledgement must not rewrite an existing
    # payload or claim a second derived write through the batch path.
    assert (
        registry.append_events_if_new(
            [
                {
                    "event_id": "batch-1",
                    "session_id": "batch-session",
                    "event_type": "assistant_output",
                    "payload": {"text": "conflicting replay"},
                }
            ]
        )
        == 0
    )
    assert registry.get_event("batch-1").payload == {"text": "first"}


def test_postgres_preview_projection_keeps_preferred_newest_candidate(
    postgres_control_store,
):
    """A late nonpreferred or older event must not clobber the fleet preview."""
    control_path, _ = postgres_control_store
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="host-3", display_name="Host", kind="test")
    registry.create_session(
        host_id="host-3", harness="codex", command="codex", session_id="preview-session"
    )
    for event_id, event_type, text, seq in (
        ("assistant-new", "assistant_output", "assistant reply", 30),
        ("user-preferred", "user_input", "the current task", 5),
        ("assistant-late", "assistant_output", "later assistant reply", 99),
        ("user-old", "user_input", "stale task", 4),
        ("user-new", "user_input", "the latest task", 6),
    ):
        registry.append_event(
            session_id="preview-session",
            event_id=event_id,
            event_type=event_type,
            payload={"text": text},
            content_preview=text,
            seq=seq,
        )

    with control_plane_connection(control_path) as con:
        assert con.execute(
            "SELECT event_id, content_preview FROM harness_session_previews WHERE session_id = ?",
            ["preview-session"],
        ).fetchone() == ("user-new", "the latest task")


def test_postgres_split_payload_keeps_event_and_transcript_response_contracts(
    postgres_control_store,
):
    """Splitting physical storage must be invisible to existing registry callers."""
    control_path, _ = postgres_control_store
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="host-4", display_name="Host", kind="test")
    registry.create_session(
        host_id="host-4", harness="codex", command="codex", session_id="payload-session"
    )
    event = registry.append_event(
        session_id="payload-session",
        event_id="payload-event",
        event_type="assistant_output",
        payload={"text": "the complete archived envelope stays readable"},
        content_preview="the complete archived envelope stays readable",
        seq=1,
    )

    assert event.payload == {"text": "the complete archived envelope stays readable"}
    assert registry.get_event("payload-event").payload == event.payload
    assert registry.list_events("payload-session")[0].payload == event.payload
    assert registry.list_events_after("payload-session", 0)[0].payload == event.payload
    assert (
        registry.list_event_page("payload-session", limit=10).events[0].payload
        == event.payload
    )
    assert registry.transcript_text("payload-session") == (
        "[assistant] the complete archived envelope stays readable"
    )
    registry.append_event(
        session_id="payload-session",
        event_id="empty-hot-event",
        event_type="assistant_output",
        payload={},
        seq=2,
    )
    empty_hot = registry.get_event("empty-hot-event")
    assert empty_hot is not None
    assert empty_hot.payload == {}
    assert empty_hot.payload_status.state == "hot"


def test_outbox_reclaims_stable_batch_and_publishes_only_manifested_parquet(
    postgres_control_store,
):
    """A retry can reuse bytes safely without exposing an unacknowledged glob."""
    control_path, parquet_dir = postgres_control_store
    from drover.server.control_outbox import (
        acknowledge_outbox_batch,
        claim_outbox_batch,
        register_published_harness_events_relation,
        publish_outbox_batch,
        published_batches,
    )
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="host-5", display_name="Host", kind="test")
    registry.create_session(
        host_id="host-5", harness="codex", command="codex", session_id="outbox-session"
    )
    for event_id in ("outbox-1", "outbox-2"):
        registry.append_event(
            session_id="outbox-session",
            event_id=event_id,
            event_type="assistant_output",
            payload={"text": event_id},
            content_preview=event_id,
        )

    started = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    with control_plane_connection(control_path) as con:
        first = claim_outbox_batch(
            con, owner="worker-a", limit=10, lease_seconds=1, now=started
        )
        assert first is not None
        # Crash before publish: claimed raw files are not analytically visible.
        assert published_batches(con) == []
        recovered = claim_outbox_batch(
            con,
            owner="worker-b",
            limit=10,
            lease_seconds=30,
            now=started + timedelta(seconds=2),
        )
        assert recovered is not None
        assert recovered.batch_id == first.batch_id
        assert recovered.event_ids == first.event_ids

        published = publish_outbox_batch(
            con, recovered, parquet_dir=parquet_dir, now=started + timedelta(seconds=3)
        )
        assert Path(published.archive_path).exists()
        # Crash after publication before acknowledgement keeps one manifest row,
        # and retrying publication returns the same immutable batch identity.
        assert [batch.batch_id for batch in published_batches(con)] == [
            published.batch_id
        ]
        retried = publish_outbox_batch(
            con, recovered, parquet_dir=parquet_dir, now=started + timedelta(seconds=4)
        )
        assert retried.batch_id == published.batch_id
        assert acknowledge_outbox_batch(con, published.batch_id) is True
        assert acknowledge_outbox_batch(con, published.batch_id) is True

        with duckdb.connect(":memory:") as analytics:
            assert register_published_harness_events_relation(analytics, con) == (
                "harness_exported_events"
            )
            assert analytics.execute(
                "SELECT event_id FROM harness_exported_events ORDER BY event_id"
            ).fetchall() == [("outbox-1",), ("outbox-2",)]

        assert con.execute(
            "SELECT state, count(*) FROM control_outbox_events GROUP BY state"
        ).fetchall() == [("acknowledged", 2)]


def test_outbox_claims_a_late_old_event_after_newer_work_was_acknowledged(
    postgres_control_store,
):
    """Commit membership, not a maximum event id or timestamp, drives export."""
    control_path, parquet_dir = postgres_control_store
    from drover.server.control_outbox import (
        acknowledge_outbox_batch,
        claim_outbox_batch,
        publish_outbox_batch,
    )
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="late-host", display_name="Host", kind="test")
    registry.create_session(
        host_id="late-host", harness="codex", command="codex", session_id="late-session"
    )
    registry.append_event(
        session_id="late-session",
        event_id="newer-committed-first",
        event_type="assistant_output",
        payload={"text": "new"},
        created_at=datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
    )
    with control_plane_connection(control_path) as con:
        first = claim_outbox_batch(con, owner="worker", limit=10)
        assert first is not None
        assert acknowledge_outbox_batch(
            con, publish_outbox_batch(con, first, parquet_dir=parquet_dir).batch_id
        )
        registry.append_event(
            session_id="late-session",
            event_id="older-committed-late",
            event_type="assistant_output",
            payload={"text": "old but late"},
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        late = claim_outbox_batch(con, owner="worker", limit=10)
        assert late is not None
        assert late.event_ids == ("older-committed-late",)


def test_retention_requires_export_usage_and_verified_archive_replay(
    postgres_control_store,
):
    """A terminal event remains hot until all replay and usage dependencies finish."""
    control_path, parquet_dir = postgres_control_store
    from drover.server.control_outbox import (
        LocalVerifiedArchiveResolver,
        acknowledge_outbox_batch,
        claim_outbox_batch,
        prune_verified_payloads,
        publish_outbox_batch,
        published_batches,
    )
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.harness.usage_rollup import rollup_pending_sessions

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="host-6", display_name="Host", kind="test")
    registry.create_session(
        host_id="host-6",
        harness="codex",
        command="codex",
        session_id="retention-session",
    )
    registry.append_event(
        session_id="retention-session",
        event_id="retention-event",
        event_type="assistant_output",
        payload={"text": "replay this exact envelope"},
        content_preview="replay this exact envelope",
        seq=1,
    )
    with control_plane_connection(control_path) as con:
        assert prune_verified_payloads(con, resolver=None) == {
            "pruned": 0,
            "protected_active": 1,
            "protected_dependency": 0,
            "verification_failed": 0,
        }
    registry.update_session_status("retention-session", "completed")

    with control_plane_connection(control_path) as con:
        assert prune_verified_payloads(con, resolver=None) == {
            "pruned": 0,
            "protected_active": 0,
            "protected_dependency": 1,
            "verification_failed": 0,
        }
        claim = claim_outbox_batch(con, owner="worker", limit=10)
        assert claim is not None
        published = publish_outbox_batch(con, claim, parquet_dir=parquet_dir)
        assert acknowledge_outbox_batch(con, published.batch_id)

        # Export alone is not enough: usage rollup has not consumed the event.
        assert prune_verified_payloads(con, resolver=None) == {
            "pruned": 0,
            "protected_active": 0,
            "protected_dependency": 1,
            "verification_failed": 0,
        }
        rollup_pending_sessions(con)

        class CorruptResolver:
            def resolve(self, **_kwargs):
                return '{"text":"tampered"}'

        assert prune_verified_payloads(con, resolver=CorruptResolver()) == {
            "pruned": 0,
            "protected_active": 0,
            "protected_dependency": 0,
            "verification_failed": 1,
        }
        resolver = LocalVerifiedArchiveResolver(
            parquet_dir,
            manifest_reader=lambda: {
                batch.batch_id for batch in published_batches(con)
            },
        )
        assert prune_verified_payloads(con, resolver=resolver)["pruned"] == 1
        assert registry.lookup_event_payload("retention-event").state == "unavailable"
        recovered = registry.lookup_event_payload("retention-event", resolver=resolver)
        assert recovered.state == "archive"
        assert recovered.payload_json == '{"text":"replay this exact envelope"}'
        unavailable = registry.list_event_page("retention-session", limit=10).events[0]
        assert unavailable.payload_status.state == "unavailable"
        assert unavailable.payload_status.reason == "archive_resolver_required"
        assert unavailable.wire_payload()["payload_unavailable"] == {
            "reason": "archive_resolver_required"
        }
        corrupt = registry.list_event_page(
            "retention-session", limit=10, resolver=CorruptResolver()
        ).events[0]
        assert corrupt.payload_status.state == "unavailable"
        assert corrupt.payload_status.reason == "archive_verification_failed"
        # Task 3's API reader receives only this injected worker resolver. Its
        # normal event paginator must replay an archived row without any raw
        # archive glob or host-path access.
        page = registry.list_event_page(
            "retention-session", limit=10, resolver=resolver
        )
        assert page.events[0].payload == {"text": "replay this exact envelope"}
        assert page.events[0].payload_status.state == "archive"


def test_archived_paginator_releases_the_only_postgres_slot_before_worker_rpc(
    postgres_single_connection_control_store,
):
    """A worker call cannot reserve API pool capacity while it waits on cold history."""
    control_path = postgres_single_connection_control_store
    from drover.server.control_outbox import payload_sha256
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry

    payload_json = '{"text":"cold bytes from the worker"}'
    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="single-slot-host", display_name="Host", kind="test")
    registry.create_session(
        host_id="single-slot-host",
        harness="codex",
        command="codex",
        session_id="single-slot-session",
    )
    registry.append_event(
        session_id="single-slot-session",
        event_id="single-slot-event",
        event_type="assistant_output",
        payload={"text": "cold bytes from the worker"},
        seq=1,
    )
    with control_plane_connection(control_path) as con:
        con.execute(
            "DELETE FROM harness_event_payloads WHERE event_id = ?",
            ["single-slot-event"],
        )
        con.execute(
            """
            INSERT INTO harness_event_archives
              (event_id, batch_id, payload_sha256, verified_at, payload_pruned_at)
            VALUES (?, 'single-slot-batch', ?, now(), now())
            """,
            ["single-slot-event", payload_sha256(payload_json)],
        )

    class PoolProbeResolver:
        def resolve(self, **_kwargs):
            # This has one possible slot. It would time out if the registry
            # invoked the worker before closing its reference-read connection.
            with control_plane_connection(control_path) as probe:
                assert probe.execute("SELECT 1").fetchone() == (1,)
            return payload_json

    page = registry.list_event_page(
        "single-slot-session", limit=10, resolver=PoolProbeResolver()
    )
    assert page.events[0].payload == {"text": "cold bytes from the worker"}
    assert page.events[0].payload_status.state == "archive"
