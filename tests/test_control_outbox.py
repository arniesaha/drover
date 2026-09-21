"""PostgreSQL control-event outbox and archive lifecycle tests."""

from __future__ import annotations

import os
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


@pytest.fixture
def postgres_control_store(tmp_path: Path, monkeypatch):
    dsn = os.environ.get("DROVER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("DROVER_TEST_POSTGRES_DSN is required for PostgreSQL integration")

    from drover.config import ControlStoreConfig
    from drover.schema import bootstrap
    from drover.server.control_store import close_control_store, configure_control_store

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
    from drover.schema import bootstrap
    from drover.server.control_store import close_control_store, configure_control_store

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


def _seed_outbox_claim(control_path: Path, *, event_id: str):
    """Create one claimed, unmanifested event for publication fault tests."""
    from drover.server.control_outbox import claim_outbox_batch
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(
        host_id=f"{event_id}-host", display_name="Export", kind="test"
    )
    registry.create_session(
        host_id=f"{event_id}-host",
        harness="codex",
        command="codex",
        session_id=f"{event_id}-session",
    )
    registry.append_event(
        session_id=f"{event_id}-session",
        event_id=event_id,
        event_type="assistant_output",
        payload={"text": f"durable immutable {event_id}"},
        seq=1,
    )
    with control_plane_connection(control_path) as con:
        claim = claim_outbox_batch(con, owner="publisher", limit=10)
        assert claim is not None
        return claim


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


def test_postgres_batch_and_structured_writers_roll_back_all_event_side_effects(
    postgres_control_store, monkeypatch
):
    """Both multi-event paths retain their all-or-nothing completion boundary."""
    control_path, _ = postgres_control_store
    from drover.server.db import control_plane_connection
    from drover.server.harness import registry as registry_module
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(
        host_id="writer-rollback-host", display_name="Host", kind="test"
    )
    for session_id in ("batch-rollback", "structured-rollback"):
        registry.create_session(
            host_id="writer-rollback-host",
            harness="codex",
            command="codex",
            session_id=session_id,
            mode="structured",
        )
    actual_enqueue = registry_module._enqueue_recap_if_completion

    def crash_after_recap(*args, **kwargs):
        actual_enqueue(*args, **kwargs)
        raise RuntimeError("simulated post-recap writer failure")

    monkeypatch.setattr(
        registry_module, "_enqueue_recap_if_completion", crash_after_recap
    )
    with pytest.raises(RuntimeError, match="post-recap writer failure"):
        registry.append_events_if_new(
            [
                {
                    "event_id": "batch-rollback-event",
                    "session_id": "batch-rollback",
                    "event_type": "status",
                    "payload": {"turn_complete": True},
                    "seq": 1,
                }
            ]
        )
    with pytest.raises(RuntimeError, match="post-recap writer failure"):
        registry.ingest_structured_events(
            [
                {
                    "event_id": "structured-rollback-event",
                    "session_id": "structured-rollback",
                    "event_type": "status",
                    "payload": {"turn_complete": True},
                    "seq": 1,
                }
            ]
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
        publish_outbox_batch,
        published_batches,
        register_published_harness_events_relation,
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


def test_outbox_rejects_a_stale_final_path_before_it_becomes_manifested(
    postgres_control_store,
):
    """A crash-retry filename is not evidence that its immutable facts are valid."""
    control_path, parquet_dir = postgres_control_store
    from drover.server.control_outbox import (
        PUBLISHED_BATCHES_DIR,
        claim_outbox_batch,
        publish_outbox_batch,
        published_batches,
    )
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="stale-host", display_name="Host", kind="test")
    registry.create_session(
        host_id="stale-host",
        harness="codex",
        command="codex",
        session_id="stale-session",
    )
    registry.append_event(
        session_id="stale-session",
        event_id="stale-event",
        event_type="assistant_output",
        payload={"text": "the actual claimed envelope"},
        seq=1,
    )

    with control_plane_connection(control_path) as con:
        claim = claim_outbox_batch(con, owner="worker", limit=10)
        assert claim is not None
        stale_path = parquet_dir / PUBLISHED_BATCHES_DIR / f"{claim.batch_id}.parquet"
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({"event_id": ["wrong-event"]}), stale_path)

        with pytest.raises(RuntimeError, match="does not match claimed membership"):
            publish_outbox_batch(con, claim, parquet_dir=parquet_dir)

        assert published_batches(con) == []
        assert con.execute(
            "SELECT state, content_sha256, archive_path FROM control_outbox_batches WHERE batch_id = ?",
            [claim.batch_id],
        ).fetchone() == ("claimed", None, None)


def test_outbox_file_sync_failure_never_manifests_acknowledges_or_prunes(
    postgres_control_store, monkeypatch
):
    """A file-durability failure leaves the claimed payload fully protected."""
    control_path, parquet_dir = postgres_control_store
    import drover.server.control_outbox as outbox
    from drover.server.control_outbox import (
        acknowledge_outbox_batch,
        prune_verified_payloads,
        publish_outbox_batch,
        published_batches,
    )
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry

    claim = _seed_outbox_claim(control_path, event_id="file-sync-event")
    actual_fsync = os.fsync

    def fail_regular_file_sync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("injected file sync failure")
        actual_fsync(descriptor)

    # The reviewed publisher never touched os.fsync, so this must be RED
    # before the durable publisher is implemented.
    monkeypatch.setattr(outbox, "os", os, raising=False)
    monkeypatch.setattr(os, "fsync", fail_regular_file_sync)
    with control_plane_connection(control_path) as con:
        with pytest.raises(OSError, match="injected file sync failure"):
            publish_outbox_batch(con, claim, parquet_dir=parquet_dir)
        assert published_batches(con) == []
        assert acknowledge_outbox_batch(con, claim.batch_id) is False
        assert con.execute(
            "SELECT state, content_sha256, archive_path FROM control_outbox_batches WHERE batch_id = ?",
            [claim.batch_id],
        ).fetchone() == ("claimed", None, None)

    HarnessRegistry(control_path).update_session_status(
        "file-sync-event-session", "completed"
    )
    retention = prune_verified_payloads(control_path, resolver=None)
    assert retention["pruned"] == 0
    assert retention["protected_dependency"] == 1


def test_outbox_directory_sync_failure_retries_existing_final_before_manifesting(
    postgres_control_store, monkeypatch
):
    """A final entry survives a failed directory sync only as an unmanifested retry."""
    control_path, parquet_dir = postgres_control_store
    import drover.server.control_outbox as outbox
    from drover.server.control_outbox import (
        PUBLISHED_BATCHES_DIR,
        acknowledge_outbox_batch,
        publish_outbox_batch,
        published_batches,
    )
    from drover.server.db import control_plane_connection

    claim = _seed_outbox_claim(control_path, event_id="directory-sync-event")
    path = parquet_dir / PUBLISHED_BATCHES_DIR / f"{claim.batch_id}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    actual_fsync = os.fsync

    def fail_directory_sync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected directory sync failure")
        actual_fsync(descriptor)

    monkeypatch.setattr(outbox, "os", os, raising=False)
    with monkeypatch.context() as failure:
        failure.setattr(os, "fsync", fail_directory_sync)
        with control_plane_connection(control_path) as con:
            with pytest.raises(OSError, match="injected directory sync failure"):
                publish_outbox_batch(con, claim, parquet_dir=parquet_dir)
            assert published_batches(con) == []
            assert acknowledge_outbox_batch(con, claim.batch_id) is False
            assert con.execute(
                "SELECT state, content_sha256, archive_path FROM control_outbox_batches WHERE batch_id = ?",
                [claim.batch_id],
            ).fetchone() == ("claimed", None, None)
    assert path.is_file()
    crash_hash = outbox._file_sha256(path)

    def fail_existing_file_sync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("injected existing file sync failure")
        actual_fsync(descriptor)

    with monkeypatch.context() as failure:
        failure.setattr(os, "fsync", fail_existing_file_sync)
        with control_plane_connection(control_path) as con:
            with pytest.raises(OSError, match="injected existing file sync failure"):
                publish_outbox_batch(con, claim, parquet_dir=parquet_dir)
            assert published_batches(con) == []

    with control_plane_connection(control_path) as con:
        published = publish_outbox_batch(con, claim, parquet_dir=parquet_dir)
        assert published.content_sha256 == crash_hash
        assert acknowledge_outbox_batch(con, published.batch_id) is True


def test_outbox_syncs_file_and_directory_before_sql_manifest(
    postgres_control_store, monkeypatch
):
    """The SQL receipt cannot become visible before the durable final entry."""
    control_path, parquet_dir = postgres_control_store
    import drover.server.control_outbox as outbox
    from drover.server.control_outbox import publish_outbox_batch
    from drover.server.db import control_plane_connection

    claim = _seed_outbox_claim(control_path, event_id="sync-order-event")
    (parquet_dir / outbox.PUBLISHED_BATCHES_DIR).mkdir(parents=True, exist_ok=True)
    actual_fsync = os.fsync
    synced: list[str] = []

    def record_sync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        synced.append("file" if stat.S_ISREG(mode) else "directory")
        actual_fsync(descriptor)

    class OrderedConnection:
        dialect = "postgres"

        def __init__(self, wrapped):
            self._wrapped = wrapped

        def execute(self, query, *args, **kwargs):
            if "UPDATE control_outbox_batches" in str(
                query
            ) and "SET state = 'published'" in str(query):
                assert "file" in synced
                assert synced[-1] == "directory"
            return self._wrapped.execute(query, *args, **kwargs)

    monkeypatch.setattr(outbox, "os", os, raising=False)
    monkeypatch.setattr(os, "fsync", record_sync)
    with control_plane_connection(control_path) as con:
        receipt = publish_outbox_batch(
            OrderedConnection(con), claim, parquet_dir=parquet_dir
        )
    assert receipt.batch_id == claim.batch_id


def test_outbox_fails_closed_without_supported_no_clobber_rename(
    postgres_control_store, monkeypatch
):
    """Unsupported platforms leave a new claim unmanifested rather than replace."""
    control_path, parquet_dir = postgres_control_store
    import drover.server.control_outbox as outbox
    from drover.server.control_outbox import publish_outbox_batch, published_batches
    from drover.server.db import control_plane_connection

    claim = _seed_outbox_claim(control_path, event_id="unsupported-rename-event")
    monkeypatch.setattr(outbox, "_EXCLUSIVE_RENAME", None, raising=False)
    monkeypatch.setattr(outbox, "_EXCLUSIVE_RENAME_FLAG", 0, raising=False)
    with control_plane_connection(control_path) as con:
        with pytest.raises(OSError, match="exclusive outbox publication unsupported"):
            publish_outbox_batch(con, claim, parquet_dir=parquet_dir)
        assert published_batches(con) == []
        assert con.execute(
            "SELECT state, content_sha256 FROM control_outbox_batches WHERE batch_id = ?",
            [claim.batch_id],
        ).fetchone() == ("claimed", None)


def test_outbox_overlapping_leases_keep_the_first_immutable_winner(
    postgres_control_store, monkeypatch
):
    """An expired publisher cannot share a temp name or overwrite the new lease's final."""
    control_path, parquet_dir = postgres_control_store
    import drover.server.control_outbox as outbox
    from drover.server.control_outbox import (
        claim_outbox_batch,
        publish_outbox_batch,
        published_batches,
    )
    from drover.server.db import control_plane_connection

    started = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    with control_plane_connection(control_path) as con:
        first = _seed_outbox_claim(control_path, event_id="overlap-event")
        # The helper claims with the default lease; make expiry deterministic.
        con.execute(
            "UPDATE control_outbox_batches SET lease_until = ? WHERE batch_id = ?",
            [started + timedelta(seconds=1), first.batch_id],
        )
    entered = threading.Event()
    release = threading.Event()
    temporary_names: list[str] = []
    original_rename = getattr(outbox, "_rename_noreplace_at", None)

    def pause_first_rename(source_fd, source_name, destination_fd, destination_name):
        temporary_names.append(source_name)
        if threading.current_thread().name == "outbox-old-lease":
            entered.set()
            assert release.wait(timeout=5)
        assert original_rename is not None
        return original_rename(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(
        outbox, "_rename_noreplace_at", pause_first_rename, raising=False
    )
    old_result: list[object] = []
    old_errors: list[BaseException] = []

    def publish_old_lease() -> None:
        try:
            with control_plane_connection(control_path) as con:
                old_result.append(
                    publish_outbox_batch(
                        con, first, parquet_dir=parquet_dir, now=started
                    )
                )
        except BaseException as exc:  # surfaced in the main test thread
            old_errors.append(exc)

    old_thread = threading.Thread(target=publish_old_lease, name="outbox-old-lease")
    old_thread.start()
    assert entered.wait(timeout=5)
    with control_plane_connection(control_path) as con:
        recovered = claim_outbox_batch(
            con,
            owner="outbox-new-lease",
            limit=10,
            lease_seconds=30,
            now=started + timedelta(seconds=2),
        )
        assert recovered is not None
        assert recovered.batch_id == first.batch_id
        new_result = publish_outbox_batch(
            con, recovered, parquet_dir=parquet_dir, now=started + timedelta(seconds=2)
        )
    winner_path = Path(new_result.archive_path)
    winner_hash = outbox._file_sha256(winner_path)
    release.set()
    old_thread.join(timeout=5)
    assert not old_thread.is_alive()
    assert old_errors == []
    assert len(old_result) == 1
    assert old_result[0].content_sha256 == winner_hash
    assert len(set(temporary_names)) == 2
    assert outbox._file_sha256(winner_path) == winner_hash
    assert list(winner_path.parent.glob(f".{winner_path.name}.*.tmp")) == []
    with control_plane_connection(control_path) as con:
        assert [batch.batch_id for batch in published_batches(con)] == [first.batch_id]


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
    """A terminal event remains hot until export, usage, and recap finish."""
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
        mode="structured",
    )
    registry.append_event(
        session_id="retention-session",
        event_id="retention-event",
        event_type="assistant_output",
        payload={"text": "replay this exact envelope"},
        content_preview="replay this exact envelope",
        seq=1,
    )
    registry.append_event(
        session_id="retention-session",
        event_id="retention-complete",
        event_type="status",
        payload={"turn_complete": True},
        seq=2,
    )
    assert prune_verified_payloads(control_path, resolver=None) == {
        "pruned": 0,
        "protected_active": 2,
        "protected_dependency": 0,
        "verification_failed": 0,
    }
    registry.update_session_status("retention-session", "completed")

    with control_plane_connection(control_path) as con:
        claim = claim_outbox_batch(con, owner="worker", limit=10)
        assert claim is not None
        published = publish_outbox_batch(con, claim, parquet_dir=parquet_dir)
        assert acknowledge_outbox_batch(con, published.batch_id)
        rollup_pending_sessions(con)

    # Export and usage alone are insufficient: the completion event left a
    # durable recap job pending at the terminal source sequence.
    assert prune_verified_payloads(control_path, resolver=None) == {
        "pruned": 0,
        "protected_active": 0,
        "protected_dependency": 2,
        "verification_failed": 0,
    }
    with control_plane_connection(control_path) as con:
        assert con.execute(
            "SELECT desired_source_seq, status FROM live_recap_jobs WHERE session_id = ?",
            ["retention-session"],
        ).fetchone() == (2, "pending")
        con.execute("""
            INSERT INTO live_session_recaps (session_id, recap_text, source_seq, generated_at)
            VALUES ('retention-session', 'done', 2, now())
            """)
        con.execute(
            "UPDATE live_recap_jobs SET status = 'done' WHERE session_id = ?",
            ["retention-session"],
        )

    class CorruptResolver:
        def resolve(self, **_kwargs):
            return '{"text":"tampered"}'

    assert prune_verified_payloads(control_path, resolver=CorruptResolver()) == {
        "pruned": 0,
        "protected_active": 0,
        "protected_dependency": 0,
        "verification_failed": 2,
    }

    def manifest_ids() -> set[str]:
        with control_plane_connection(control_path) as manifest_con:
            return {batch.batch_id for batch in published_batches(manifest_con)}

    resolver = LocalVerifiedArchiveResolver(parquet_dir, manifest_reader=manifest_ids)
    assert prune_verified_payloads(control_path, resolver=resolver)["pruned"] == 2
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
    page = registry.list_event_page("retention-session", limit=10, resolver=resolver)
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


def test_retention_releases_the_only_postgres_slot_before_archive_resolution(
    postgres_single_connection_control_store,
):
    """Retention resolves detached references, then conditionally reopens a slot."""
    control_path = postgres_single_connection_control_store
    from drover.server.control_outbox import (
        acknowledge_outbox_batch,
        claim_outbox_batch,
        prune_verified_payloads,
        publish_outbox_batch,
    )
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.harness.usage_rollup import rollup_pending_sessions

    registry = HarnessRegistry(control_path)
    registry.register_host(
        host_id="retention-slot-host", display_name="Host", kind="test"
    )
    registry.create_session(
        host_id="retention-slot-host",
        harness="codex",
        command="codex",
        session_id="retention-slot-session",
        mode="structured",
    )
    registry.append_event(
        session_id="retention-slot-session",
        event_id="retention-slot-event",
        event_type="assistant_output",
        payload={"text": "worker RPC must not hold the sole slot"},
        seq=1,
    )
    registry.append_event(
        session_id="retention-slot-session",
        event_id="retention-slot-complete",
        event_type="status",
        payload={"turn_complete": True},
        seq=2,
    )
    registry.update_session_status("retention-slot-session", "completed")
    with control_plane_connection(control_path) as con:
        claim = claim_outbox_batch(con, owner="worker", limit=10)
        assert claim is not None
        assert acknowledge_outbox_batch(
            con,
            publish_outbox_batch(
                con, claim, parquet_dir=control_path.parent / "retention-slot-parquet"
            ).batch_id,
        )
        rollup_pending_sessions(con)
        con.execute("""
            INSERT INTO live_session_recaps (session_id, recap_text, source_seq, generated_at)
            VALUES ('retention-slot-session', 'done', 2, now())
            """)
        con.execute(
            "UPDATE live_recap_jobs SET status = 'done' WHERE session_id = ?",
            ["retention-slot-session"],
        )

    payloads = {
        "retention-slot-event": '{"text":"worker RPC must not hold the sole slot"}',
        "retention-slot-complete": '{"turn_complete":true}',
    }

    class ChangesDependencyDuringResolver:
        def resolve(self, *, event_id, **_kwargs):
            # The candidate query must have returned this sole pool slot before
            # Task 3's worker RPC is entered.
            with control_plane_connection(control_path) as probe:
                assert probe.execute("SELECT 1").fetchone() == (1,)
                probe.execute(
                    "UPDATE harness_sessions SET status = 'active' WHERE session_id = ?",
                    ["retention-slot-session"],
                )
            return payloads[event_id]

    # A resolver may run long enough for a dependency to change. The second,
    # fresh transaction must refuse both deletions after it observes that fact.
    assert prune_verified_payloads(
        control_path, resolver=ChangesDependencyDuringResolver()
    ) == {
        "pruned": 0,
        "protected_active": 0,
        "protected_dependency": 2,
        "verification_failed": 0,
    }
    with control_plane_connection(control_path) as con:
        assert con.execute(
            "SELECT count(*) FROM harness_event_payloads"
        ).fetchone() == (2,)
        assert con.execute(
            "SELECT count(*) FROM harness_event_archives"
        ).fetchone() == (0,)
        con.execute(
            "UPDATE harness_sessions SET status = 'completed' WHERE session_id = ?",
            ["retention-slot-session"],
        )

    class PoolProbeResolver:
        def resolve(self, *, event_id, **_kwargs):
            with control_plane_connection(control_path) as probe:
                assert probe.execute("SELECT 1").fetchone() == (1,)
            return payloads[event_id]

    assert prune_verified_payloads(control_path, resolver=PoolProbeResolver()) == {
        "pruned": 2,
        "protected_active": 0,
        "protected_dependency": 0,
        "verification_failed": 0,
    }


def test_reopened_session_usage_retries_until_archived_payloads_are_verified(
    postgres_control_store,
):
    """Cold historical usage cannot replace an exact total with a partial one."""
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
    from drover.server.harness.recap_worker import LiveRecapWorker
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.harness.usage_rollup import (
        UsageRollupWorker,
        rollup_pending_sessions,
    )

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="reopen-host", display_name="Reopen", kind="test")
    registry.create_session(
        host_id="reopen-host",
        harness="claude-code",
        command="claude",
        session_id="reopen-session",
        mode="structured",
    )
    registry.append_event(
        session_id="reopen-session",
        event_id="reopen-history",
        event_type="assistant_output",
        content_preview="earlier bounded recap context",
        payload={
            "native_event_id": "reopen-history-native",
            "usage": {"input_tokens": 10, "output_tokens": 1},
        },
        seq=1,
    )
    registry.append_event(
        session_id="reopen-session",
        event_id="reopen-complete",
        event_type="status",
        payload={"turn_complete": True},
        seq=2,
    )
    registry.update_session_status("reopen-session", "completed")

    with control_plane_connection(control_path) as con:
        claim = claim_outbox_batch(con, owner="worker", limit=10)
        assert claim is not None
        assert acknowledge_outbox_batch(
            con, publish_outbox_batch(con, claim, parquet_dir=parquet_dir).batch_id
        )
        assert rollup_pending_sessions(con).rolled == 1
        con.execute("""INSERT INTO live_session_recaps
               (session_id, recap_text, source_seq, generated_at)
               VALUES ('reopen-session', 'done', 2, now())""")
        con.execute(
            "UPDATE live_recap_jobs SET status = 'done' WHERE session_id = ?",
            ["reopen-session"],
        )

    def manifest_ids() -> set[str]:
        with control_plane_connection(control_path) as manifest_con:
            return {batch.batch_id for batch in published_batches(manifest_con)}

    resolver = LocalVerifiedArchiveResolver(parquet_dir, manifest_reader=manifest_ids)
    assert prune_verified_payloads(control_path, resolver=resolver)["pruned"] == 2
    registry.mark_session_recovered("reopen-session", "native-reopen")
    registry.append_event(
        session_id="reopen-session",
        event_id="reopen-new",
        event_type="assistant_output",
        content_preview="later bounded recap context",
        payload={
            "native_event_id": "reopen-new-native",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
        seq=3,
    )

    unavailable = UsageRollupWorker(
        duckdb_path=control_path, archive_resolver=None
    ).drain_once()
    assert (unavailable.rolled, unavailable.incomplete_sessions) == (0, 1)
    with control_plane_connection(control_path) as con:
        # The old exact total is retained until every archive member is
        # verified. It is never overwritten by only the newly reopened turn.
        assert con.execute(
            "SELECT input_tokens, output_tokens, exact, source_event_count "
            "FROM session_usage WHERE session_id = ?",
            ["reopen-session"],
        ).fetchone() == (10, 1, True, 2)

    recovered = UsageRollupWorker(
        duckdb_path=control_path, archive_resolver=resolver
    ).drain_once()
    assert (recovered.rolled, recovered.incomplete_sessions) == (1, 0)
    with control_plane_connection(control_path) as con:
        assert con.execute(
            "SELECT input_tokens, output_tokens, exact, source_event_count "
            "FROM session_usage WHERE session_id = ?",
            ["reopen-session"],
        ).fetchone() == (15, 3, True, 3)

    # Recaps consume the retained bounded preview projection, not an invented
    # raw payload. A reopened generation still sees both history and new work.
    previews = LiveRecapWorker(duckdb_path=control_path)._load_events("reopen-session")
    assert [item["content_preview"] for item in previews] == [
        "earlier bounded recap context",
        "later bounded recap context",
    ]
