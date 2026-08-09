"""Observed cockpit activity analytics contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.cockpit.analytics import AnalyticsFilters, activity_analytics


def _analytics_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE spans_enriched (
          span_id VARCHAR,
          session_id VARCHAR,
          start_time TIMESTAMPTZ,
          duration_ms DOUBLE,
          harness VARCHAR,
          llm_provider VARCHAR,
          llm_model VARCHAR,
          agent_model VARCHAR,
          repo_owner VARCHAR,
          repo_name VARCHAR,
          total_tokens BIGINT,
          prompt_tokens BIGINT,
          completion_tokens BIGINT,
          cost_usd DOUBLE,
          cache_read_tokens BIGINT,
          cache_write_tokens BIGINT
        );
        CREATE TABLE sessions (
          session_id VARCHAR,
          agent_id VARCHAR,
          task_id VARCHAR,
          started_at TIMESTAMPTZ,
          ended_at TIMESTAMPTZ
        );
        CREATE TABLE harness_sessions (
          session_id VARCHAR,
          host_id VARCHAR,
          harness VARCHAR,
          repo_owner VARCHAR,
          repo_name VARCHAR,
          model VARCHAR,
          started_at TIMESTAMPTZ,
          ended_at TIMESTAMPTZ,
          updated_at TIMESTAMPTZ
        );
        """)
    return con


def _insert_session(
    con: duckdb.DuckDBPyConnection,
    *,
    session_id: str,
    project: str,
    host: str,
    harness: str,
    tokens: int | None,
    cost: float | None = None,
    cache_read: int | None = None,
) -> None:
    owner, name = project.split("/", 1)
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    con.execute(
        """
        INSERT INTO harness_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [session_id, host, harness, owner, name, "model-a", now, now, now],
    )
    con.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        [session_id, f"agent-{host}", None, now, now],
    )
    con.execute(
        """
        INSERT INTO spans_enriched VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        [
            f"span-{session_id}",
            session_id,
            now,
            100.0 if tokens is not None else None,
            harness,
            "anthropic",
            "model-a",
            None,
            owner,
            name,
            tokens,
            None,
            None,
            cost,
            cache_read,
            None,
        ],
    )


@pytest.fixture
def low_coverage_analytics_db():
    con = _analytics_connection()
    _insert_session(
        con,
        session_id="alpha-1",
        project="acme/alpha",
        host="mac-mini",
        harness="claude-code",
        tokens=100,
        cost=0.4,
        cache_read=20,
    )
    _insert_session(
        con,
        session_id="alpha-2",
        project="acme/alpha",
        host="nas",
        harness="openclaw",
        tokens=None,
    )
    _insert_session(
        con,
        session_id="beta-1",
        project="acme/beta",
        host="nas",
        harness="openclaw",
        tokens=None,
    )
    yield con
    con.close()


def test_project_ranking_falls_back_when_token_coverage_is_low(
    low_coverage_analytics_db,
):
    result = activity_analytics(low_coverage_analytics_db, AnalyticsFilters(days=7))

    assert result.project_metric == "sessions"
    assert result.coverage.token_percent == pytest.approx(100 / 3)
    assert result.projects[0].project_key == "acme/alpha"
    assert result.projects[0].session_count == 2
    assert result.projects[0].harnesses == ("claude-code", "openclaw")
    assert result.projects[0].hosts == ("mac-mini", "nas")
    assert result.totals.session_count == 3
    assert result.totals.total_tokens == 100


def test_project_ranking_uses_tokens_at_eighty_percent_coverage():
    con = _analytics_connection()
    try:
        for index, tokens in enumerate((10, 10, 10, None), start=1):
            _insert_session(
                con,
                session_id=f"alpha-{index}",
                project="acme/alpha",
                host="mac-mini",
                harness="codex",
                tokens=tokens,
            )
        _insert_session(
            con,
            session_id="beta-1",
            project="acme/beta",
            host="nas",
            harness="codex",
            tokens=100,
        )

        result = activity_analytics(con, AnalyticsFilters(days=7))
    finally:
        con.close()

    assert result.coverage.token_percent == 80.0
    assert result.project_metric == "tokens"
    assert result.projects[0].project_key == "acme/beta"


def test_analytics_filters_host_harness_provider_model_and_project(
    low_coverage_analytics_db,
):
    result = activity_analytics(
        low_coverage_analytics_db,
        AnalyticsFilters(
            days=7,
            host_id="mac-mini",
            harness="claude-code",
            provider="anthropic",
            model="model-a",
            project_key="acme/alpha",
        ),
    )

    assert result.totals.session_count == 1
    assert [project.project_key for project in result.projects] == ["acme/alpha"]
    assert [item.key for item in result.hosts] == ["mac-mini"]


def test_analytics_counts_harness_sessions_without_spans():
    con = _analytics_connection()
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    con.execute(
        """
        INSERT INTO harness_sessions VALUES (
          'local-only', 'mac-mini', 'gemini', 'acme', 'offline',
          'gemini-2.5', ?, NULL, ?
        )
        """,
        [now, now],
    )
    try:
        result = activity_analytics(con, AnalyticsFilters(days=7))
    finally:
        con.close()

    assert result.totals.session_count == 1
    assert result.projects[0].project_key == "acme/offline"
    assert result.project_metric == "sessions"
    assert result.coverage.token_percent == 0.0


def test_analytics_handles_empty_bootstrapped_lakehouse(tmp_path: Path):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        result = activity_analytics(con, AnalyticsFilters(days=7))
    finally:
        con.close()

    assert result.totals.session_count == 0
    assert result.projects == ()
    assert result.coverage.attributable_session_percent == 0.0


def test_analytics_bounds_harness_sessions_by_latest_activity():
    con = _analytics_connection()
    now = datetime.now(timezone.utc)
    con.execute(
        """
        INSERT INTO harness_sessions VALUES (
          'long-running', 'mac-mini', 'codex', 'acme', 'active',
          'gpt-5', ?, NULL, ?
        )
        """,
        [now - timedelta(days=30), now - timedelta(hours=1)],
    )
    try:
        result = activity_analytics(con, AnalyticsFilters(days=7))
    finally:
        con.close()

    assert result.totals.session_count == 1


@pytest.mark.parametrize("days", [0, -1, 366])
def test_analytics_rejects_unbounded_day_ranges(days):
    with pytest.raises(ValueError, match="days"):
        AnalyticsFilters(days=days)
