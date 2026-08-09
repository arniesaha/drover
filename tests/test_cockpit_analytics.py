"""Observed cockpit activity analytics contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    FindingCandidate,
    FindingEvidence,
    Severity,
)
from drover.server.cockpit.analytics import (
    AnalyticsCursorCodec,
    AnalyticsFilters,
    activity_analytics,
)
from drover.server.cockpit.service import CockpitService


def _analytics_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE spans_enriched (
          span_id VARCHAR,
          session_id VARCHAR,
          start_time TIMESTAMPTZ,
          end_time TIMESTAMPTZ,
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
    model: str = "model-a",
    cost: float | None = None,
    cache_read: int | None = None,
) -> None:
    owner, name = project.split("/", 1)
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    con.execute(
        """
        INSERT INTO harness_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [session_id, host, harness, owner, name, model, now, now, now],
    )
    con.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        [session_id, f"agent-{host}", None, now, now],
    )
    con.execute(
        """
        INSERT INTO spans_enriched VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        [
            f"span-{session_id}",
            session_id,
            now,
            now,
            100.0 if tokens is not None else None,
            harness,
            "anthropic",
            model,
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


def test_analytics_freshness_uses_recent_span_end_for_old_started_session():
    con = _analytics_connection()
    old = datetime.now(timezone.utc) - timedelta(days=30)
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    _insert_session(
        con,
        session_id="long-span",
        project="acme/long-span",
        host="mac-mini",
        harness="codex",
        tokens=10,
    )
    con.execute(
        "UPDATE harness_sessions SET started_at=?, ended_at=?, updated_at=?",
        [old, old, old],
    )
    con.execute("UPDATE sessions SET started_at=?, ended_at=?", [old, old])
    con.execute("UPDATE spans_enriched SET start_time=?, end_time=?", [old, recent])
    try:
        result = activity_analytics(con, AnalyticsFilters(days=7))
    finally:
        con.close()

    assert result.totals.session_count == 1
    assert result.metadata.observed_at == recent
    assert result.metadata.freshness == "fresh"


def test_paginated_dimension_freshness_uses_harness_and_session_latest_activity():
    con = _analytics_connection()
    old = datetime.now(timezone.utc) - timedelta(days=2)
    harness_recent = datetime.now(timezone.utc) - timedelta(minutes=4)
    session_recent = datetime.now(timezone.utc) - timedelta(minutes=3)
    for session_id, host in (
        ("old", "host-a-old"),
        ("harness", "host-harness-recent"),
        ("session", "host-session-recent"),
    ):
        _insert_session(
            con,
            session_id=session_id,
            project=f"acme/{session_id}",
            host=host,
            harness="codex",
            tokens=10,
        )
        con.execute(
            "UPDATE harness_sessions SET started_at=?, ended_at=?, updated_at=? WHERE session_id=?",
            [old, old, old, session_id],
        )
        con.execute(
            "UPDATE sessions SET started_at=?, ended_at=? WHERE session_id=?",
            [old, old, session_id],
        )
        con.execute(
            "UPDATE spans_enriched SET start_time=?, end_time=? WHERE session_id=?",
            [old, old, session_id],
        )
    con.execute(
        "UPDATE harness_sessions SET updated_at=? WHERE session_id='harness'",
        [harness_recent],
    )
    con.execute(
        "UPDATE sessions SET ended_at=? WHERE session_id='session'",
        [session_recent],
    )
    codec = AnalyticsCursorCodec(b"test-cursor-secret")
    pages = []
    cursor = None
    try:
        while True:
            page = activity_analytics(
                con,
                AnalyticsFilters(days=7, limit=1, host_cursor=cursor),
                cursor_codec=codec,
            )
            pages.extend(page.hosts)
            cursor = page.pagination.hosts.next_cursor
            if cursor is None:
                break
    finally:
        con.close()

    hosts = {item.key: item for item in pages}
    assert hosts["host-harness-recent"].metadata.observed_at == harness_recent
    assert hosts["host-harness-recent"].metadata.freshness == "fresh"
    assert hosts["host-session-recent"].metadata.observed_at == session_recent
    assert hosts["host-session-recent"].metadata.freshness == "fresh"


@pytest.mark.parametrize("days", [0, -1, 366])
def test_analytics_rejects_unbounded_day_ranges(days):
    with pytest.raises(ValueError, match="days"):
        AnalyticsFilters(days=days)


def test_analytics_pages_each_dimension_deterministically_without_overlap():
    con = _analytics_connection()
    try:
        for index in range(5):
            _insert_session(
                con,
                session_id=f"session-{index}",
                project=f"acme/project-{index}",
                host=f"host-{index}",
                harness=f"harness-{index}",
                tokens=100 - index,
                model=f"model-{index}",
            )
        codec = AnalyticsCursorCodec(b"test-cursor-secret")
        first = activity_analytics(
            con, AnalyticsFilters(days=7, limit=2), cursor_codec=codec
        )
        second = activity_analytics(
            con,
            AnalyticsFilters(
                days=7,
                limit=2,
                project_cursor=first.pagination.projects.next_cursor,
                harness_cursor=first.pagination.harnesses.next_cursor,
                host_cursor=first.pagination.hosts.next_cursor,
                model_cursor=first.pagination.models.next_cursor,
            ),
            cursor_codec=codec,
        )
    finally:
        con.close()

    assert [item.project_key for item in first.projects] == [
        "acme/project-0",
        "acme/project-1",
    ]
    assert not (
        {item.project_key for item in first.projects}
        & {item.project_key for item in second.projects}
    )
    assert first.pagination.projects.limit == 2
    assert first.pagination.projects.next_cursor
    assert not (
        {item.key for item in first.hosts} & {item.key for item in second.hosts}
    )
    assert not (
        {item.key for item in first.harnesses} & {item.key for item in second.harnesses}
    )
    assert not (
        {item.key for item in first.models} & {item.key for item in second.models}
    )


def test_analytics_cursor_is_bound_to_dimension_filters_and_sort():
    con = _analytics_connection()
    try:
        for index in range(3):
            _insert_session(
                con,
                session_id=f"bound-{index}",
                project=f"acme/project-{index}",
                host=f"host-{index}",
                harness="codex",
                tokens=10,
            )
        codec = AnalyticsCursorCodec(b"test-cursor-secret")
        first = activity_analytics(
            con, AnalyticsFilters(days=7, limit=1), cursor_codec=codec
        )
        cursor = first.pagination.projects.next_cursor

        with pytest.raises(ValueError, match="cursor does not match"):
            activity_analytics(
                con,
                AnalyticsFilters(days=30, limit=1, project_cursor=cursor),
                cursor_codec=codec,
            )
        with pytest.raises(ValueError, match="cursor does not match"):
            activity_analytics(
                con,
                AnalyticsFilters(days=7, limit=1, host_cursor=cursor),
                cursor_codec=codec,
            )
        with pytest.raises(ValueError, match="invalid analytics cursor"):
            activity_analytics(
                con,
                AnalyticsFilters(days=7, limit=1, project_cursor=cursor + "tampered"),
                cursor_codec=codec,
            )
    finally:
        con.close()


@pytest.mark.parametrize("limit", [0, 101, True])
def test_analytics_rejects_unbounded_page_limits(limit):
    with pytest.raises(ValueError, match="limit"):
        AnalyticsFilters(limit=limit)


def test_observed_aggregates_name_source_freshness_and_coverage(
    low_coverage_analytics_db,
):
    result = activity_analytics(
        low_coverage_analytics_db,
        AnalyticsFilters(days=7),
        cursor_codec=AnalyticsCursorCodec(b"test-cursor-secret"),
    )

    assert result.metadata.source == "drover_observed"
    assert result.metadata.observed_at is not None
    assert result.metadata.freshness in {"fresh", "stale"}
    assert result.totals.metadata.coverage.token_percent == pytest.approx(100 / 3)
    assert result.projects[0].metadata.source == "drover_observed"
    assert result.harnesses[0].metadata.observed_at == result.metadata.observed_at
    harnesses = {item.key: item for item in result.harnesses}
    assert harnesses["claude-code"].metadata.coverage.token_percent == 100.0
    assert harnesses["openclaw"].metadata.coverage.token_percent == 0.0


class _FailingProviderService:
    def latest_accounts(self):
        raise RuntimeError("provider offline")


def test_cockpit_overview_isolates_provider_failure(low_coverage_analytics_db):
    service = CockpitService(
        duckdb_path=None,
        provider_usage=_FailingProviderService(),
        connect=lambda: low_coverage_analytics_db,
    )

    payload = service.overview(AnalyticsFilters(days=7))

    assert payload["provider_capacity"]["status"] == "error"
    assert payload["provider_capacity"]["data"] == []
    assert payload["activity"]["status"] == "ok"
    assert (
        payload["activity"]["observed_at"]
        == payload["activity"]["data"]["metadata"]["observed_at"]
    )
    assert payload["popular_projects"][0]["metric"] == "sessions"
    assert payload["popular_projects"][0]["project_key"] == "acme/alpha"


def test_cockpit_analytics_isolates_activity_failure():
    service = CockpitService(
        duckdb_path=None,
        provider_usage=None,
        connect=lambda: (_ for _ in ()).throw(RuntimeError("duckdb offline")),
    )

    payload = service.analytics(AnalyticsFilters(days=7))

    assert payload["provider_capacity"]["status"] == "unavailable"
    assert payload["activity"]["status"] == "error"
    assert payload["activity"]["data"] is None


def test_cockpit_overview_counts_actionable_insights_by_severity(tmp_path):
    db_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db_path)
    repository = AdvisoryRepository(db_path)
    now = datetime.now(timezone.utc)
    for index, severity in enumerate((Severity.CRITICAL, Severity.HIGH, Severity.LOW)):
        repository.observe(
            FindingCandidate(
                analyzer_id="deterministic.connector_freshness",
                rule_id=f"connector.rule.{index}",
                target_type="provider_connector",
                target_id=f"mac-mini/openai/account-{index}",
                analyzer_class=AnalyzerClass.DETERMINISTIC,
                severity=severity,
                confidence=Confidence.CONFIRMED,
                title=f"Finding {index}",
                impact="Capacity may be stale.",
                remediation=("Refresh the connector.",),
                evidence=(
                    FindingEvidence(
                        source_ref=f"provider:{index}",
                        observed_at=now,
                        fields={"index": index},
                    ),
                ),
                content_hash=f"hash-{index}",
            ),
            run_id="run-1",
        )

    payload = CockpitService(
        duckdb_path=db_path,
        provider_usage=None,
        connect=lambda: (_ for _ in ()).throw(RuntimeError("activity offline")),
        advisory_repository=repository,
    ).overview(AnalyticsFilters(days=7))

    assert payload["insight_counts"] == {
        "critical": 1,
        "high": 1,
        "medium": 0,
        "low": 1,
    }
    assert payload["activity"]["status"] == "error"


def test_cockpit_overview_isolates_insight_count_failure(
    low_coverage_analytics_db,
):
    class _FailedRepository:
        def list_findings(self):
            raise RuntimeError("advisory database unavailable")

    payload = CockpitService(
        duckdb_path=None,
        provider_usage=None,
        connect=lambda: low_coverage_analytics_db,
        advisory_repository=_FailedRepository(),
    ).overview(AnalyticsFilters(days=7))

    assert payload["insight_counts"] is None
    assert payload["activity"]["status"] == "ok"


def test_swift_contract_fixture_matches_backend_overview_shape(tmp_path):
    db_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db_path)
    fixture_path = (
        Path(__file__).parents[1]
        / "apps/drover/DroverKit/Tests/DroverKitTests/Fixtures"
        / "cockpit-overview-with-insights.json"
    )

    actual = CockpitService(
        duckdb_path=db_path,
        provider_usage=None,
        connect=lambda: (_ for _ in ()).throw(RuntimeError("activity offline")),
    ).overview(AnalyticsFilters(days=7))

    assert actual == json.loads(fixture_path.read_text())
