"""Runtime orchestration tests for the PostgreSQL harness-event exporter."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

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
        schema=f"drover_task3_export_{uuid4().hex}",
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


def _seed_event(control_path: Path) -> None:
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="export-host", display_name="Export", kind="test")
    registry.create_session(
        host_id="export-host",
        harness="codex",
        command="codex",
        session_id="export-session",
    )
    registry.append_event(
        session_id="export-session",
        event_id="export-event",
        event_type="assistant_output",
        payload={"text": "bounded durable export"},
        seq=1,
    )


def test_export_worker_recovers_a_claim_and_rebuilds_the_manifest_relation(
    postgres_control_store,
):
    """A restart resumes an expired claim without treating it as zero lag."""
    control_path, parquet_dir = postgres_control_store
    from drover.server.control_exporter import ControlOutboxExporter
    from drover.server.control_outbox import claim_outbox_batch, outbox_status
    from drover.server.db import control_plane_connection, open_duckdb_connection

    _seed_event(control_path)
    now = datetime.now(timezone.utc)
    with control_plane_connection(control_path) as con:
        claim = claim_outbox_batch(
            con, owner="crashed-worker", lease_seconds=1, now=now
        )
        assert claim is not None
        status = outbox_status(con)
        assert status["pending"] == 0
        assert status["claimed"] == 1
        assert status["oldest_outstanding_at"] is not None

    exporter = ControlOutboxExporter(
        control_path=control_path,
        analytical_path=control_path.parent / "analytics.duckdb",
        parquet_dir=parquet_dir,
        batch_size=8,
        flush_age_seconds=60,
        lease_seconds=1,
    )
    result = exporter.run_once(now=now + timedelta(seconds=2))
    assert result["published"] == 1
    assert result["acknowledged"] == 1
    assert result["retention_pruned"] == 0
    assert result["retention_verification_failed"] == 0
    with control_plane_connection(control_path) as con:
        assert outbox_status(con)["acknowledged"] == 1
    analytics = open_duckdb_connection(control_path.parent / "analytics.duckdb")
    try:
        assert analytics.execute(
            "SELECT event_id FROM harness_exported_events"
        ).fetchall() == [("export-event",)]
    finally:
        analytics.close()


def test_export_worker_acknowledges_a_published_batch_after_a_restart(
    postgres_control_store,
):
    """SQL-visible immutable bytes are related before the delayed acknowledgement."""
    control_path, parquet_dir = postgres_control_store
    from drover.server.control_exporter import ControlOutboxExporter
    from drover.server.control_outbox import (
        claim_outbox_batch,
        outbox_status,
        publish_outbox_batch,
    )
    from drover.server.db import control_plane_connection

    _seed_event(control_path)
    with control_plane_connection(control_path) as con:
        claim = claim_outbox_batch(con, owner="crashed-worker", limit=8)
        assert claim is not None
        publish_outbox_batch(con, claim, parquet_dir=parquet_dir)
        assert outbox_status(con)["published_unacknowledged"] == 1

    exporter = ControlOutboxExporter(
        control_path=control_path,
        analytical_path=control_path.parent / "analytics.duckdb",
        parquet_dir=parquet_dir,
        batch_size=8,
        flush_age_seconds=60,
    )
    result = exporter.run_once()
    assert result["published"] == 0
    assert result["acknowledged"] == 1
    assert result["retention_pruned"] == 0
    with control_plane_connection(control_path) as con:
        assert outbox_status(con)["published_unacknowledged"] == 0
        assert outbox_status(con)["acknowledged"] == 1
