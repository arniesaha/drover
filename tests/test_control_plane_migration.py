"""Moving the control-plane tables out of the analytical store must be safe.

Issue #95. The live hub has 3 hosts, 120 sessions and 41,649 events sitting
inside a 640.8 MB ``drover.duckdb``. Relocating them happens on a running
fleet, on first start, with no maintenance window, so the migration has to be
idempotent, has to lose nothing, and must never let a stale legacy row
overwrite something the control plane has since written.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.db import (
    CONTROL_PLANE_TABLES,
    close_control_plane_connections,
    control_plane_path,
)
from drover.server.harness.registry import HarnessRegistry


@pytest.fixture(autouse=True)
def _release_pins():
    yield
    close_control_plane_connections()


def _legacy_lakehouse(tmp_path: Path) -> Path:
    """A store shaped like every deployed hub: harness tables in the main file."""
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    control_plane_path(duckdb_path).unlink()

    con = duckdb.connect(str(duckdb_path))
    try:
        from drover.server.harness.schema import bootstrap_harness_tables

        bootstrap_harness_tables(con)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_session_recaps (
              session_id VARCHAR PRIMARY KEY, recap_text VARCHAR NOT NULL,
              source_seq INTEGER NOT NULL, generator_model VARCHAR,
              generated_at TIMESTAMP NOT NULL DEFAULT now())
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_recap_jobs (
              session_id VARCHAR PRIMARY KEY, desired_source_seq INTEGER NOT NULL,
              status VARCHAR NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              last_error VARCHAR, enqueued_at TIMESTAMP NOT NULL DEFAULT now(),
              updated_at TIMESTAMP NOT NULL DEFAULT now(), next_run_at TIMESTAMP,
              stream_publish_needed BOOLEAN NOT NULL DEFAULT FALSE)
        """)
        con.execute(
            "INSERT INTO harness_hosts (host_id, display_name, kind, status, "
            "capabilities_json) VALUES ('mac-mini', 'Mac mini', 'darwin', "
            "'online', '{}')"
        )
        con.execute(
            "INSERT INTO harness_sessions (session_id, host_id, harness, command, "
            "status) VALUES ('legacy-1', 'mac-mini', 'claude', 'claude', 'running')"
        )
        con.execute(
            "INSERT INTO harness_events (event_id, session_id, event_type, "
            "payload_json, seq) VALUES ('e1', 'legacy-1', 'user_input', "
            '\'{"text":"hi"}\', 1)'
        )
        con.execute(
            "INSERT INTO live_session_recaps (session_id, recap_text, source_seq) "
            "VALUES ('legacy-1', 'said hi', 1)"
        )
    finally:
        con.close()
    return duckdb_path


def _control_plane_rows(duckdb_path: Path, table: str) -> list[tuple]:
    con = duckdb.connect(str(control_plane_path(duckdb_path)))
    try:
        return con.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    finally:
        con.close()


def test_first_start_moves_existing_control_plane_rows_into_their_own_store(tmp_path):
    """The fleet must still be there after the deploy that splits the store."""
    duckdb_path = _legacy_lakehouse(tmp_path)

    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)

    assert [row[0] for row in _control_plane_rows(duckdb_path, "harness_hosts")] == [
        "mac-mini"
    ]
    assert [row[0] for row in _control_plane_rows(duckdb_path, "harness_sessions")] == [
        "legacy-1"
    ]
    assert [row[0] for row in _control_plane_rows(duckdb_path, "harness_events")] == [
        "e1"
    ]
    assert [
        row[0] for row in _control_plane_rows(duckdb_path, "live_session_recaps")
    ] == ["legacy-1"]
    assert HarnessRegistry(duckdb_path).get_session("legacy-1") is not None


def test_running_the_migration_again_neither_duplicates_nor_drops_rows(tmp_path):
    """Every start runs it, so the second one has to be a no-op."""
    duckdb_path = _legacy_lakehouse(tmp_path)
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    first = {
        table: _control_plane_rows(duckdb_path, table) for table in CONTROL_PLANE_TABLES
    }

    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)

    assert {
        table: _control_plane_rows(duckdb_path, table) for table in CONTROL_PLANE_TABLES
    } == first


def test_a_stale_legacy_row_never_overwrites_live_control_plane_state(tmp_path):
    """After the move the registry is authoritative; the old copy is frozen.

    A restart re-runs the migration, and the legacy ``harness_sessions`` row
    still says ``running`` long after the session ended.
    """
    duckdb_path = _legacy_lakehouse(tmp_path)
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.update_session_status("legacy-1", "completed")

    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)

    session = registry.get_session("legacy-1")
    assert session is not None and session.status == "completed"


def test_the_legacy_tables_are_left_in_place_for_a_rollback(tmp_path):
    """Reverting the deploy has to be a restart, not a data-recovery exercise.

    #104 shipped and did not hold. The next change to this area should be able
    to go backwards without anyone having to reconstruct 41,649 events.
    """
    duckdb_path = _legacy_lakehouse(tmp_path)

    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)

    con = duckdb.connect(str(duckdb_path))
    try:
        assert con.execute("SELECT count(*) FROM harness_sessions").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM harness_events").fetchone()[0] == 1
    finally:
        con.close()


def test_a_fresh_install_starts_with_an_empty_control_plane_store(tmp_path):
    """Nothing to migrate must not be an error."""
    duckdb_path = tmp_path / "drover.duckdb"

    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)

    assert control_plane_path(duckdb_path).exists()
    assert _control_plane_rows(duckdb_path, "harness_sessions") == []
