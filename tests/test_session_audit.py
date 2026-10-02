"""Tests for read-only session consistency auditing."""

from __future__ import annotations

from pathlib import Path

import duckdb
from click.testing import CliRunner
from memory_helpers import put_summary

from drover.server.__main__ import main
from drover.session_audit import audit_session_consistency, format_session_audit


def _create_base_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE TABLE agent_events (
            session_id VARCHAR,
            agent_id VARCHAR,
            task_id VARCHAR,
            timestamp TIMESTAMP
        )
        """)


def _create_sessions_view(con: duckdb.DuckDBPyConnection) -> None:
    # Summaries are not part of the view: they live in PostgreSQL (#480).
    con.execute("""
        CREATE VIEW sessions AS
        SELECT
          e.session_id,
          any_value(e.agent_id) AS agent_id,
          any_value(e.task_id) AS task_id,
          min(e.timestamp) AS started_at,
          max(e.timestamp) AS ended_at,
          count(*) AS event_count
        FROM agent_events e
        GROUP BY e.session_id
        """)


def test_session_audit_clean_view_backed_state(pg_control_path: Path) -> None:
    for session_id in ("s1", "s2"):
        put_summary(pg_control_path, session_id, next_steps_md="next")
    con = duckdb.connect(":memory:")
    try:
        _create_base_tables(con)
        con.execute("""
            INSERT INTO agent_events VALUES
              ('s1', 'agent-a', 't1', '2026-05-24 10:00:00'),
              ('s1', 'agent-a', 't1', '2026-05-24 10:01:00'),
              ('s2', 'agent-b', 't2', '2026-05-24 11:00:00')
            """)
        _create_sessions_view(con)

        report = audit_session_consistency(con, memory_store_path=pg_control_path)
    finally:
        con.close()

    assert report["sessions_relation_type"] == "VIEW"
    assert report["status"] == "ok"
    assert report["is_clean"] is True
    assert report["event_sessions"] == 2
    assert report["sessions_rows"] == 2
    assert report["event_sessions_missing_session_row"] == 0
    assert report["session_rows_without_events"] == 0
    assert report["event_count_mismatches"] == 0
    assert report["event_sessions_without_summary"] == 0
    assert report["summaries_without_events"] == 0
    assert report["summaries_available"] is True
    assert report["warnings"] == []


def test_session_audit_reports_missing_summaries_without_marking_drift(
    pg_control_path: Path,
) -> None:
    put_summary(pg_control_path, "s1", next_steps_md="next")
    con = duckdb.connect(":memory:")
    try:
        _create_base_tables(con)
        con.execute("""
            INSERT INTO agent_events VALUES
              ('s1', 'agent-a', 't1', '2026-05-24 10:00:00'),
              ('s2', 'agent-a', 't2', '2026-05-24 11:00:00')
            """)
        _create_sessions_view(con)

        report = audit_session_consistency(con, memory_store_path=pg_control_path)
    finally:
        con.close()

    assert report["sessions_relation_type"] == "VIEW"
    assert report["status"] == "ok"
    assert report["is_clean"] is True
    assert report["event_sessions_without_summary"] == 1
    assert report["summaries_without_events"] == 0
    assert report["warnings"] == []


def test_session_audit_detects_orphan_summaries_as_drift(pg_control_path: Path) -> None:
    for session_id in ("s1", "orphan"):
        put_summary(pg_control_path, session_id, next_steps_md="next")
    con = duckdb.connect(":memory:")
    try:
        _create_base_tables(con)
        con.execute("""
            INSERT INTO agent_events VALUES
              ('s1', 'agent-a', 't1', '2026-05-24 10:00:00'),
              ('s2', 'agent-a', 't2', '2026-05-24 11:00:00')
            """)
        _create_sessions_view(con)

        report = audit_session_consistency(con, memory_store_path=pg_control_path)
    finally:
        con.close()

    assert report["sessions_relation_type"] == "VIEW"
    assert report["status"] == "drift"
    assert report["is_clean"] is False
    assert report["event_sessions_without_summary"] == 1
    assert report["summaries_without_events"] == 1
    assert any("session/summary drift" in warning for warning in report["warnings"])


def test_session_audit_without_a_memory_store_skips_summary_coverage(
    tmp_path: Path,
) -> None:
    """No PostgreSQL store: summary coverage is unknown, not drift, not a crash."""
    con = duckdb.connect(":memory:")
    try:
        _create_base_tables(con)
        con.execute("INSERT INTO agent_events VALUES ('s1', 'agent-a', 't1', now())")
        _create_sessions_view(con)

        report = audit_session_consistency(
            con, memory_store_path=tmp_path / "drover.duckdb"
        )
    finally:
        con.close()

    assert report["status"] == "ok"
    assert report["summaries_available"] is False
    assert report["event_sessions"] == 1
    assert report["event_sessions_without_summary"] is None
    assert report["summaries_without_events"] is None
    assert any("PostgreSQL" in warning for warning in report["warnings"])


def test_session_audit_detects_legacy_sessions_base_table() -> None:
    con = duckdb.connect(":memory:")
    try:
        _create_base_tables(con)
        con.execute("""
            INSERT INTO agent_events VALUES
              ('s1', 'agent-a', 't1', '2026-05-24 10:00:00'),
              ('s1', 'agent-a', 't1', '2026-05-24 10:01:00'),
              ('s2', 'agent-a', 't2', '2026-05-24 11:00:00')
            """)
        con.execute("CREATE TABLE sessions (session_id VARCHAR, event_count INTEGER)")
        con.execute("INSERT INTO sessions VALUES ('s1', 1), ('stale', 1)")

        report = audit_session_consistency(con)
    finally:
        con.close()

    assert report["sessions_relation_type"] == "BASE TABLE"
    assert report["status"] == "legacy_base_table"
    assert report["is_clean"] is False
    assert report["event_sessions_missing_session_row"] == 1
    assert report["session_rows_without_events"] == 1
    assert report["event_count_mismatches"] == 1
    assert any("legacy base table" in warning for warning in report["warnings"])
    assert "Back up" in report["remediation"]


def test_format_session_audit_includes_remediation_for_legacy_table() -> None:
    text = format_session_audit(
        {
            "duckdb_path": "/tmp/drover.duckdb",
            "sessions_relation_type": "BASE TABLE",
            "status": "legacy_base_table",
            "is_clean": False,
            "event_sessions": 2,
            "sessions_rows": 1,
            "event_sessions_missing_session_row": 1,
            "session_rows_without_events": 0,
            "event_count_mismatches": 0,
            "event_sessions_without_summary": 0,
            "summaries_without_events": 0,
            "warnings": ["sessions is a legacy base table"],
            "remediation": "Back up the DuckDB file, then rebuild sessions as the canonical view by running schema bootstrap from a reviewed maintenance window.",
        }
    )

    assert "Drover session consistency audit" in text
    assert "sessions relation : BASE TABLE" in text
    assert "legacy_base_table" in text
    assert "remediation:" in text
    assert "Back up the DuckDB file" in text


def test_cli_audit_sessions_json_exits_nonzero_on_legacy_table(tmp_path) -> None:
    db = tmp_path / "legacy.duckdb"
    con = duckdb.connect(str(db))
    try:
        _create_base_tables(con)
        con.execute("INSERT INTO agent_events VALUES ('s1', 'agent-a', 't1', now())")
        con.execute("CREATE TABLE sessions (session_id VARCHAR, event_count INTEGER)")
        con.execute("INSERT INTO sessions VALUES ('s1', 1)")
    finally:
        con.close()

    result = CliRunner().invoke(main, ["audit-sessions", "--db", str(db), "--json"])

    assert result.exit_code == 2
    assert '"sessions_relation_type": "BASE TABLE"' in result.output
    assert '"status": "legacy_base_table"' in result.output
    assert "Back up" in result.output
