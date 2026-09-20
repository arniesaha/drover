"""Contract tests for the opt-in PostgreSQL central control store."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from drover.config import load_config


@pytest.fixture
def postgres_control_store(tmp_path: Path, monkeypatch):
    """One disposable PostgreSQL schema, registered for one central path."""
    dsn = os.environ.get("DROVER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("DROVER_TEST_POSTGRES_DSN is required for PostgreSQL integration")

    from drover.config import ControlStoreConfig
    from drover.server.control_store import close_control_store, configure_control_store
    from drover.schema import bootstrap_control_plane_store

    schema = f"drover_test_{uuid4().hex}"
    monkeypatch.setenv("DROVER_TEST_POSTGRES_DSN", dsn)
    control_path = tmp_path / "central.duckdb"
    config = ControlStoreConfig(
        backend="postgres",
        dsn_env="DROVER_TEST_POSTGRES_DSN",
        pool_min_size=1,
        pool_max_size=2,
        acquire_timeout_seconds=0.15,
        statement_timeout_seconds=1.0,
        schema=schema,
    )
    configure_control_store(control_path, config)
    bootstrap_control_plane_store(control_path)
    try:
        yield control_path, config
    finally:
        close_control_store(control_path)
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_postgres_control_store_requires_a_secret_environment_name(tmp_path: Path):
    """A config file contains an env *name*, never a PostgreSQL DSN."""
    config_path = tmp_path / "postgres.toml"
    config_path.write_text(
        "[control_store]\n"
        "backend = 'postgres'\n"
        "dsn_env = ''\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="control_store.dsn_env"):
        load_config(config_path)


def test_postgres_control_store_rejects_an_invalid_pool_range(tmp_path: Path):
    config_path = tmp_path / "postgres.toml"
    config_path.write_text(
        "[control_store]\n"
        "backend = 'postgres'\n"
        "dsn_env = 'DROVER_TEST_POSTGRES_DSN'\n"
        "pool_min_size = 4\n"
        "pool_max_size = 2\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="pool_min_size"):
        load_config(config_path)


def test_qmark_binder_preserves_question_marks_in_sql_literals_and_comments():
    """The adapter changes parameters, not arbitrary SQL syntax."""
    from drover.server.control_store import bind_qmark_parameters

    sql = "SELECT '?', value FROM events -- ?\nWHERE event_id = ? /* ? */"

    assert bind_qmark_parameters(sql) == (
        "SELECT '?', value FROM events -- ?\nWHERE event_id = %s /* ? */"
    )


def test_qmark_binder_keeps_dollar_literals_and_escapes_percent_for_psycopg():
    from drover.server.control_store import bind_qmark_parameters

    sql = "SELECT $$?%$$, payload ?? 'flag' FROM events WHERE label LIKE '%hot%' AND id=?"

    assert bind_qmark_parameters(sql) == (
        "SELECT $$?%%$$, payload ? 'flag' FROM events "
        "WHERE label LIKE '%%hot%%' AND id=%s"
    )


def test_control_store_registration_is_scoped_to_one_central_path(tmp_path: Path):
    """Configuring a hub must never redirect another host-local path."""
    from drover.config import ControlStoreConfig
    from drover.server.control_store import (
        close_control_store,
        configure_control_store,
        is_postgres_control_store,
    )

    hub = tmp_path / "hub.duckdb"
    spoke = tmp_path / "spoke.duckdb"
    config = ControlStoreConfig(
        backend="postgres",
        dsn_env="DROVER_TEST_POSTGRES_DSN",
        pool_min_size=1,
        pool_max_size=2,
        acquire_timeout_seconds=0.2,
        statement_timeout_seconds=1.0,
    )
    try:
        configure_control_store(hub, config)
        assert is_postgres_control_store(hub) is True
        assert is_postgres_control_store(spoke) is False
    finally:
        close_control_store(hub)


def test_postgres_control_store_rolls_back_a_control_plane_transaction(
    postgres_control_store,
):
    control_path, _ = postgres_control_store
    from drover.server.db import control_plane_connection

    with control_plane_connection(control_path) as con:
        con.execute("BEGIN")
        con.execute(
            "INSERT INTO harness_hosts "
            "(host_id, display_name, kind, status, capabilities_json) "
            "VALUES (?, ?, ?, ?, ?)",
            ["rollback-host", "Rollback", "test", "online", "{}"],
        )
        con.execute("ROLLBACK")

    with control_plane_connection(control_path) as con:
        assert con.execute(
            "SELECT host_id FROM harness_hosts WHERE host_id = ?", ["rollback-host"]
        ).fetchone() is None


def test_postgres_bootstrap_creates_the_registered_schema(postgres_control_store):
    _, config = postgres_control_store
    dsn = os.environ[config.dsn_env]
    import psycopg

    with psycopg.connect(dsn) as con:
        row = con.execute(
            "SELECT to_regclass(%s)", [f"{config.schema}.harness_hosts"]
        ).fetchone()

    assert row == (f"{config.schema}.harness_hosts",)


def test_postgres_bootstrap_serializes_concurrent_starters(tmp_path: Path):
    """Two API/worker starters apply one ordered schema history."""
    dsn = os.environ.get("DROVER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("DROVER_TEST_POSTGRES_DSN is required for PostgreSQL integration")

    from drover.config import ControlStoreConfig
    from drover.server.postgres_control_store import PostgresControlStore

    schema = f"drover_test_{uuid4().hex}"
    config = ControlStoreConfig(
        backend="postgres",
        dsn_env="DROVER_TEST_POSTGRES_DSN",
        pool_min_size=1,
        pool_max_size=1,
        acquire_timeout_seconds=1.0,
        statement_timeout_seconds=1.0,
        schema=schema,
    )
    starters = [PostgresControlStore(config), PostgresControlStore(config)]
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def bootstrap(store: PostgresControlStore) -> None:
        try:
            barrier.wait(timeout=2)
            store.bootstrap()
        except Exception as exc:  # pragma: no cover - asserted by parent thread
            errors.append(exc)

    threads = [threading.Thread(target=bootstrap, args=(store,)) for store in starters]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        import psycopg

        with psycopg.connect(dsn) as con:
            rows = con.execute(
                f'SELECT version FROM "{schema}".control_schema_migrations'
            ).fetchall()
        assert rows == [(1,)]
    finally:
        for store in starters:
            store.close()
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_postgres_registry_round_trip_preserves_duplicate_event_replay(
    postgres_control_store,
):
    control_path, _ = postgres_control_store
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="pg-host", display_name="PG", kind="test")
    session = registry.create_session(
        host_id="pg-host", harness="codex", command="codex", session_id="pg-session"
    )

    first = registry.append_event(
        session_id=session.session_id,
        event_id="pg-event-1",
        event_type="user_input",
        payload={"payload": {"text": "hello"}},
        seq=1,
    )
    replay = registry.append_event(
        session_id=session.session_id,
        event_id="pg-event-1",
        event_type="user_input",
        payload={"payload": {"text": "hello"}},
        seq=1,
    )

    assert replay.event_id == first.event_id == "pg-event-1"
    assert [event.event_id for event in registry.list_events(session.session_id)] == [
        "pg-event-1"
    ]


def test_postgres_recap_workers_claim_one_generation(postgres_control_store):
    """Concurrent workers retain the queue's one-generation claim fence."""
    control_path, _ = postgres_control_store
    from drover.server.db import control_plane_connection
    from drover.server.harness.recap_jobs import enqueue_live_recap
    from drover.server.harness.recap_worker import LiveRecapWorker

    with control_plane_connection(control_path) as con:
        assert enqueue_live_recap(con, "pg-recap-claim", 7)

    workers = [
        LiveRecapWorker(duckdb_path=control_path),
        LiveRecapWorker(duckdb_path=control_path),
    ]
    barrier = threading.Barrier(2)
    claims: list[object] = []

    def claim(worker: LiveRecapWorker) -> None:
        barrier.wait(timeout=2)
        result = worker._claim_due_job()
        if result is not None:
            claims.append(result)

    threads = [threading.Thread(target=claim, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(claims) == 1
    assert claims[0].session_id == "pg-recap-claim"
    assert claims[0].attempts == 1


def test_postgres_recap_completion_upserts_only_a_live_claim(postgres_control_store):
    """The completion transaction remains fenced and portable on PostgreSQL."""
    control_path, _ = postgres_control_store
    from drover.server.db import control_plane_connection
    from drover.server.harness.recap_jobs import enqueue_live_recap
    from drover.server.harness.recap_worker import LiveRecapWorker

    with control_plane_connection(control_path) as con:
        assert enqueue_live_recap(con, "pg-recap-complete", 9)

    worker = LiveRecapWorker(duckdb_path=control_path)
    claim = worker._claim_due_job()
    assert claim is not None
    assert worker._complete(claim, "PostgreSQL recap.", "test-model") is True
    assert worker._complete(claim, "Stale recap.", "test-model") is False

    with control_plane_connection(control_path) as con:
        assert con.execute(
            "SELECT recap_text, source_seq, generator_model FROM live_session_recaps "
            "WHERE session_id = ?",
            ["pg-recap-complete"],
        ).fetchone() == ("PostgreSQL recap.", 9, "test-model")
        assert con.execute(
            "SELECT status, attempts FROM live_recap_jobs WHERE session_id = ?",
            ["pg-recap-complete"],
        ).fetchone() == ("done", 1)


def test_postgres_advisory_observe_serializes_one_finding(postgres_control_store):
    """Concurrent observations preserve one finding lifecycle and every occurrence."""
    control_path, _ = postgres_control_store
    from drover.server.advisory.repository import AdvisoryRepository
    from drover.server.advisory.types import (
        AnalyzerClass,
        Confidence,
        FindingCandidate,
        FindingEvidence,
        Severity,
    )
    from drover.server.db import control_plane_connection

    candidate = FindingCandidate(
        analyzer_id="postgres",
        rule_id="concurrent.observe",
        target_type="test",
        target_id="shared-target",
        analyzer_class=AnalyzerClass.DETERMINISTIC,
        severity=Severity.HIGH,
        confidence=Confidence.CONFIRMED,
        title="Concurrent advisory observation",
        impact="The state transition must remain atomic.",
        remediation=("Keep the control-store lock.",),
        evidence=(
            FindingEvidence(
                source_ref="postgres-test",
                observed_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
                fields={"concurrent": True},
                excerpt="concurrent observation",
            ),
        ),
        content_hash="postgres-concurrent-v1",
    )
    repository = AdvisoryRepository(control_path)
    barrier = threading.Barrier(2)
    finding_ids: list[str] = []
    errors: list[Exception] = []

    def observe() -> None:
        try:
            barrier.wait(timeout=2)
            finding_ids.append(repository.observe(candidate, run_id="pg-concurrent").finding_id)
        except Exception as exc:  # pragma: no cover - asserted by parent thread
            errors.append(exc)

    threads = [threading.Thread(target=observe) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(set(finding_ids)) == 1
    with control_plane_connection(control_path) as con:
        assert con.execute("SELECT count(*) FROM advisory_findings").fetchone() == (1,)
        assert con.execute("SELECT count(*) FROM advisory_occurrences").fetchone() == (2,)


def test_postgres_snapshot_bridges_only_analytical_control_facts(
    postgres_control_store,
):
    """Analytics sees a consistent temp snapshot without event payload history."""
    control_path, _ = postgres_control_store
    from drover.server.db import attached_control_plane_snapshot, control_plane_connection
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="pg-snapshot-host", display_name="Snapshot", kind="test")
    registry.create_session(
        host_id="pg-snapshot-host",
        harness="codex",
        command="codex",
        session_id="pg-snapshot-session",
    )
    with control_plane_connection(control_path) as con:
        con.execute(
            "INSERT INTO session_usage "
            "(session_id, harness, turn_count, exact, source, source_seq, source_event_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["pg-snapshot-session", "codex", 1, True, "harness_events", 1, 1],
        )

    import duckdb

    with duckdb.connect(":memory:") as analytical:
        with attached_control_plane_snapshot(analytical, control_path):
            assert analytical.execute("SELECT host_id FROM harness_hosts").fetchall() == [
                ("pg-snapshot-host",)
            ]
            assert analytical.execute(
                "SELECT session_id FROM harness_sessions"
            ).fetchall() == [("pg-snapshot-session",)]
            assert analytical.execute(
                "SELECT session_id FROM session_usage"
            ).fetchall() == [("pg-snapshot-session",)]
            with pytest.raises(duckdb.CatalogException):
                analytical.execute("SELECT * FROM harness_events")
