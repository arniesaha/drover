"""Bounded analytics over Drover-observed normalized session facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import duckdb

_MAX_DAYS = 365
_MAX_BREAKDOWNS = 100
_TOKEN_COVERAGE_THRESHOLD = 80.0


@dataclass(frozen=True)
class AnalyticsFilters:
    days: int = 7
    host_id: str | None = None
    harness: str | None = None
    provider: str | None = None
    model: str | None = None
    project_key: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.days, bool) or not isinstance(self.days, int):
            raise ValueError("days must be an integer")
        if not 1 <= self.days <= _MAX_DAYS:
            raise ValueError(f"days must be within [1, {_MAX_DAYS}]")


@dataclass(frozen=True)
class ActivityTotals:
    session_count: int
    total_tokens: int
    cost_usd: float
    cache_read_tokens: int
    cache_write_tokens: int
    total_latency_ms: float
    average_latency_ms: float | None


@dataclass(frozen=True)
class Coverage:
    attributable_session_percent: float
    token_percent: float
    cost_percent: float
    cache_percent: float
    latency_percent: float


@dataclass(frozen=True)
class ActivityBreakdown:
    key: str
    session_count: int
    total_tokens: int
    cost_usd: float
    cache_read_tokens: int
    cache_write_tokens: int
    total_latency_ms: float
    average_latency_ms: float | None


@dataclass(frozen=True)
class ProjectActivity:
    project_key: str
    session_count: int
    total_tokens: int
    cost_usd: float
    cache_read_tokens: int
    cache_write_tokens: int
    total_latency_ms: float
    average_latency_ms: float | None
    harnesses: tuple[str, ...]
    hosts: tuple[str, ...]


@dataclass(frozen=True)
class ActivityAnalytics:
    totals: ActivityTotals
    projects: tuple[ProjectActivity, ...]
    harnesses: tuple[ActivityBreakdown, ...]
    hosts: tuple[ActivityBreakdown, ...]
    models: tuple[ActivityBreakdown, ...]
    project_metric: Literal["tokens", "sessions"]
    coverage: Coverage


def activity_analytics(
    con: duckdb.DuckDBPyConnection, filters: AnalyticsFilters
) -> ActivityAnalytics:
    """Return observed metrics without reconciling them to provider quota."""
    base_sql, params = _session_facts_sql(filters)
    aggregate = con.execute(
        base_sql + """
        SELECT
          count(*) AS session_count,
          COALESCE(sum(total_tokens), 0) AS total_tokens,
          COALESCE(sum(cost_usd), 0) AS cost_usd,
          COALESCE(sum(cache_read_tokens), 0) AS cache_read_tokens,
          COALESCE(sum(cache_write_tokens), 0) AS cache_write_tokens,
          COALESCE(sum(total_latency_ms), 0) AS total_latency_ms,
          avg(total_latency_ms) FILTER (WHERE has_latency) AS average_latency_ms,
          count(*) FILTER (WHERE project_key IS NOT NULL) AS attributable_sessions,
          count(*) FILTER (WHERE project_key IS NOT NULL AND has_tokens) AS token_sessions,
          count(*) FILTER (WHERE project_key IS NOT NULL AND has_cost) AS cost_sessions,
          count(*) FILTER (WHERE project_key IS NOT NULL AND has_cache) AS cache_sessions,
          count(*) FILTER (WHERE project_key IS NOT NULL AND has_latency) AS latency_sessions
        FROM filtered_sessions
        """,
        params,
    ).fetchone()
    assert aggregate is not None
    session_count = int(aggregate[0] or 0)
    attributable_sessions = int(aggregate[7] or 0)
    coverage = Coverage(
        attributable_session_percent=_percent(attributable_sessions, session_count),
        token_percent=_percent(int(aggregate[8] or 0), attributable_sessions),
        cost_percent=_percent(int(aggregate[9] or 0), attributable_sessions),
        cache_percent=_percent(int(aggregate[10] or 0), attributable_sessions),
        latency_percent=_percent(int(aggregate[11] or 0), attributable_sessions),
    )
    project_metric: Literal["tokens", "sessions"] = (
        "tokens" if coverage.token_percent >= _TOKEN_COVERAGE_THRESHOLD else "sessions"
    )
    totals = ActivityTotals(
        session_count=session_count,
        total_tokens=int(aggregate[1] or 0),
        cost_usd=float(aggregate[2] or 0),
        cache_read_tokens=int(aggregate[3] or 0),
        cache_write_tokens=int(aggregate[4] or 0),
        total_latency_ms=float(aggregate[5] or 0),
        average_latency_ms=(float(aggregate[6]) if aggregate[6] is not None else None),
    )
    projects = _project_breakdowns(con, base_sql, params, project_metric)
    return ActivityAnalytics(
        totals=totals,
        projects=projects,
        harnesses=_dimension_breakdowns(con, base_sql, params, "harness"),
        hosts=_dimension_breakdowns(con, base_sql, params, "host_id"),
        models=_dimension_breakdowns(con, base_sql, params, "model"),
        project_metric=project_metric,
        coverage=coverage,
    )


def _session_facts_sql(filters: AnalyticsFilters) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = [filters.days]
    for column, value in (
        ("host_id", filters.host_id),
        ("harness", filters.harness),
        ("provider", filters.provider),
        ("model", filters.model),
        ("project_key", filters.project_key),
    ):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    filter_sql = " AND ".join(where) if where else "TRUE"
    return (
        f"""
        WITH bounds AS (
          SELECT now() - CAST(? AS INTEGER) * INTERVAL '1 day' AS cutoff
        ),
        span_sessions AS (
          SELECT
            session_id,
            min(start_time) AS started_at,
            any_value(harness) FILTER (WHERE harness IS NOT NULL) AS harness,
            any_value(llm_provider) FILTER (WHERE llm_provider IS NOT NULL) AS provider,
            COALESCE(
              any_value(llm_model) FILTER (WHERE llm_model IS NOT NULL),
              any_value(agent_model) FILTER (WHERE agent_model IS NOT NULL)
            ) AS model,
            mode(repo_owner || '/' || repo_name) FILTER (
              WHERE repo_owner IS NOT NULL AND repo_name IS NOT NULL
            ) AS project_key,
            sum(
              CASE
                WHEN total_tokens IS NOT NULL THEN total_tokens
                WHEN prompt_tokens IS NOT NULL OR completion_tokens IS NOT NULL
                  THEN COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)
                ELSE NULL
              END
            ) AS total_tokens,
            sum(cost_usd) AS cost_usd,
            sum(cache_read_tokens) AS cache_read_tokens,
            sum(cache_write_tokens) AS cache_write_tokens,
            sum(duration_ms) AS total_latency_ms,
            bool_or(
              total_tokens IS NOT NULL
              OR prompt_tokens IS NOT NULL
              OR completion_tokens IS NOT NULL
            ) AS has_tokens,
            bool_or(cost_usd IS NOT NULL) AS has_cost,
            bool_or(
              cache_read_tokens IS NOT NULL OR cache_write_tokens IS NOT NULL
            ) AS has_cache,
            bool_or(duration_ms IS NOT NULL) AS has_latency
          FROM spans_enriched, bounds
          WHERE session_id IS NOT NULL
            AND start_time >= bounds.cutoff
          GROUP BY session_id
        ),
        harness_base AS (
          SELECT hs.*
          FROM harness_sessions hs, bounds
          WHERE COALESCE(hs.updated_at, hs.ended_at, hs.started_at) >= bounds.cutoff
        ),
        session_base AS (
          SELECT s.*
          FROM sessions s, bounds
          WHERE COALESCE(s.ended_at, s.started_at) >= bounds.cutoff
        ),
        session_facts AS (
          SELECT
            hs.session_id,
            COALESCE(hs.started_at, ss.started_at) AS started_at,
            hs.host_id,
            COALESCE(ss.harness, hs.harness) AS harness,
            ss.provider,
            COALESCE(ss.model, hs.model) AS model,
            COALESCE(
              ss.project_key,
              CASE
                WHEN hs.repo_owner IS NOT NULL AND hs.repo_name IS NOT NULL
                  THEN hs.repo_owner || '/' || hs.repo_name
                ELSE NULL
              END
            ) AS project_key,
            ss.total_tokens,
            ss.cost_usd,
            ss.cache_read_tokens,
            ss.cache_write_tokens,
            ss.total_latency_ms,
            COALESCE(ss.has_tokens, FALSE) AS has_tokens,
            COALESCE(ss.has_cost, FALSE) AS has_cost,
            COALESCE(ss.has_cache, FALSE) AS has_cache,
            COALESCE(ss.has_latency, FALSE) AS has_latency
          FROM harness_base hs
          LEFT JOIN span_sessions ss USING (session_id)

          UNION ALL

          SELECT
            ss.session_id, ss.started_at, NULL AS host_id, ss.harness,
            ss.provider, ss.model, ss.project_key,
            ss.total_tokens, ss.cost_usd, ss.cache_read_tokens,
            ss.cache_write_tokens, ss.total_latency_ms, ss.has_tokens,
            ss.has_cost, ss.has_cache, ss.has_latency
          FROM span_sessions ss
          WHERE NOT EXISTS (
            SELECT 1 FROM harness_base hs WHERE hs.session_id = ss.session_id
          )

          UNION ALL

          SELECT
            s.session_id, s.started_at, NULL AS host_id, NULL AS harness,
            NULL AS provider, NULL AS model, NULL AS project_key,
            NULL AS total_tokens, NULL AS cost_usd,
            NULL AS cache_read_tokens, NULL AS cache_write_tokens,
            NULL AS total_latency_ms, FALSE AS has_tokens, FALSE AS has_cost,
            FALSE AS has_cache, FALSE AS has_latency
          FROM session_base s
          WHERE NOT EXISTS (
            SELECT 1 FROM harness_base hs WHERE hs.session_id = s.session_id
          )
            AND NOT EXISTS (
              SELECT 1 FROM span_sessions ss WHERE ss.session_id = s.session_id
            )
        ),
        filtered_sessions AS (
          SELECT * FROM session_facts WHERE {filter_sql}
        )
        """,
        params,
    )


def _project_breakdowns(
    con: duckdb.DuckDBPyConnection,
    base_sql: str,
    params: list[Any],
    metric: Literal["tokens", "sessions"],
) -> tuple[ProjectActivity, ...]:
    order_column = "total_tokens" if metric == "tokens" else "session_count"
    rows = con.execute(
        base_sql + f"""
        SELECT
          project_key,
          count(*) AS session_count,
          COALESCE(sum(total_tokens), 0) AS total_tokens,
          COALESCE(sum(cost_usd), 0) AS cost_usd,
          COALESCE(sum(cache_read_tokens), 0) AS cache_read_tokens,
          COALESCE(sum(cache_write_tokens), 0) AS cache_write_tokens,
          COALESCE(sum(total_latency_ms), 0) AS total_latency_ms,
          avg(total_latency_ms) FILTER (WHERE has_latency) AS average_latency_ms,
          list(DISTINCT harness ORDER BY harness) FILTER (WHERE harness IS NOT NULL),
          list(DISTINCT host_id ORDER BY host_id) FILTER (WHERE host_id IS NOT NULL)
        FROM filtered_sessions
        WHERE project_key IS NOT NULL
        GROUP BY project_key
        ORDER BY {order_column} DESC, project_key
        LIMIT {_MAX_BREAKDOWNS}
        """,
        params,
    ).fetchall()
    return tuple(
        ProjectActivity(
            project_key=str(row[0]),
            session_count=int(row[1]),
            total_tokens=int(row[2]),
            cost_usd=float(row[3]),
            cache_read_tokens=int(row[4]),
            cache_write_tokens=int(row[5]),
            total_latency_ms=float(row[6]),
            average_latency_ms=float(row[7]) if row[7] is not None else None,
            harnesses=tuple(row[8] or ()),
            hosts=tuple(row[9] or ()),
        )
        for row in rows
    )


def _dimension_breakdowns(
    con: duckdb.DuckDBPyConnection,
    base_sql: str,
    params: list[Any],
    column: Literal["harness", "host_id", "model"],
) -> tuple[ActivityBreakdown, ...]:
    rows = con.execute(
        base_sql + f"""
        SELECT
          {column} AS key,
          count(*) AS session_count,
          COALESCE(sum(total_tokens), 0) AS total_tokens,
          COALESCE(sum(cost_usd), 0) AS cost_usd,
          COALESCE(sum(cache_read_tokens), 0) AS cache_read_tokens,
          COALESCE(sum(cache_write_tokens), 0) AS cache_write_tokens,
          COALESCE(sum(total_latency_ms), 0) AS total_latency_ms,
          avg(total_latency_ms) FILTER (WHERE has_latency) AS average_latency_ms
        FROM filtered_sessions
        WHERE {column} IS NOT NULL
        GROUP BY {column}
        ORDER BY session_count DESC, key
        LIMIT {_MAX_BREAKDOWNS}
        """,
        params,
    ).fetchall()
    return tuple(
        ActivityBreakdown(
            key=str(row[0]),
            session_count=int(row[1]),
            total_tokens=int(row[2]),
            cost_usd=float(row[3]),
            cache_read_tokens=int(row[4]),
            cache_write_tokens=int(row[5]),
            total_latency_ms=float(row[6]),
            average_latency_ms=float(row[7]) if row[7] is not None else None,
        )
        for row in rows
    )


def _percent(numerator: int, denominator: int) -> float:
    return 100.0 * numerator / denominator if denominator else 0.0
