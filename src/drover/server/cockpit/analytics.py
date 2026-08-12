"""Bounded analytics over Drover-observed normalized session facts."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets
from typing import Any, Literal

import duckdb

from drover.event_identity import canonical_agent_events_cte

_MAX_DAYS = 365
_MAX_BREAKDOWNS = 100
_DEFAULT_BREAKDOWNS = 25
_TOKEN_COVERAGE_THRESHOLD = 80.0
_FRESHNESS_SECONDS = 3600.0


class AnalyticsSnapshotChangedError(ValueError):
    """A cursor's logical activity snapshot is no longer reconstructable."""

    def __init__(self) -> None:
        super().__init__("snapshot_changed")


@dataclass(frozen=True)
class AnalyticsFilters:
    days: int = 7
    host_id: str | None = None
    harness: str | None = None
    provider: str | None = None
    model: str | None = None
    project_key: str | None = None
    limit: int = _DEFAULT_BREAKDOWNS
    project_cursor: str | None = None
    harness_cursor: str | None = None
    host_cursor: str | None = None
    model_cursor: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.days, bool) or not isinstance(self.days, int):
            raise ValueError("days must be an integer")
        if not 1 <= self.days <= _MAX_DAYS:
            raise ValueError(f"days must be within [1, {_MAX_DAYS}]")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise ValueError("limit must be an integer")
        if not 1 <= self.limit <= _MAX_BREAKDOWNS:
            raise ValueError(f"limit must be within [1, {_MAX_BREAKDOWNS}]")


@dataclass(frozen=True)
class ActivityTotals:
    session_count: int
    total_tokens: int
    cost_usd: float
    cache_read_tokens: int
    cache_write_tokens: int
    total_latency_ms: float
    average_latency_ms: float | None
    metadata: AggregateMetadata


@dataclass(frozen=True)
class Coverage:
    attributable_session_percent: float
    token_percent: float
    cost_percent: float
    cache_percent: float
    latency_percent: float


@dataclass(frozen=True)
class AggregateMetadata:
    source: Literal["drover_observed"]
    observed_at: datetime | None
    freshness: Literal["fresh", "stale", "unavailable"]
    coverage: Coverage


@dataclass(frozen=True)
class PageMetadata:
    limit: int
    next_cursor: str | None


@dataclass(frozen=True)
class AnalyticsPagination:
    projects: PageMetadata
    harnesses: PageMetadata
    hosts: PageMetadata
    models: PageMetadata


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
    metadata: AggregateMetadata


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
    metadata: AggregateMetadata


@dataclass(frozen=True)
class ActivityAnalytics:
    snapshot_at: datetime
    snapshot_version: str
    totals: ActivityTotals
    projects: tuple[ProjectActivity, ...]
    harnesses: tuple[ActivityBreakdown, ...]
    hosts: tuple[ActivityBreakdown, ...]
    models: tuple[ActivityBreakdown, ...]
    project_metric: Literal["tokens", "sessions"]
    coverage: Coverage
    metadata: AggregateMetadata
    pagination: AnalyticsPagination


class AnalyticsCursorCodec:
    """Opaque HMAC-authenticated keyset cursor bound to a query surface."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("analytics cursor secret must be at least 16 bytes")
        self._secret = secret

    def encode(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")

    def decode(self, cursor: str) -> dict[str, Any]:
        try:
            padding = "=" * (-len(cursor) % 4)
            raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
            body, signature = raw[:-32], raw[-32:]
            if len(body) == 0 or not hmac.compare_digest(
                signature, hmac.new(self._secret, body, hashlib.sha256).digest()
            ):
                raise ValueError
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid analytics cursor") from exc


_DEFAULT_CURSOR_CODEC = AnalyticsCursorCodec(secrets.token_bytes(32))


def activity_analytics(
    con: duckdb.DuckDBPyConnection,
    filters: AnalyticsFilters,
    *,
    cursor_codec: AnalyticsCursorCodec | None = None,
) -> ActivityAnalytics:
    """Return observed metrics from one DuckDB MVCC snapshot.

    When the caller already owns a transaction, that transaction defines the
    snapshot and remains caller-owned. Otherwise this function begins and ends
    a read transaction, rolling it back on every failure path.
    """
    owns_transaction = not _connection_has_active_transaction(con)
    if owns_transaction:
        con.execute("BEGIN TRANSACTION")

    try:
        result = _activity_analytics_in_snapshot(
            con, filters, cursor_codec=cursor_codec
        )
        if owns_transaction:
            con.execute("COMMIT")
        return result
    except BaseException:
        if owns_transaction:
            try:
                con.execute("ROLLBACK")
            except duckdb.TransactionException:
                # A failed COMMIT can already have closed the transaction. The
                # original exception remains the actionable failure.
                pass
        raise


def _connection_has_active_transaction(con: duckdb.DuckDBPyConnection) -> bool:
    """Detect a caller transaction without issuing a destructive nested BEGIN.

    DuckDB aborts an existing transaction when a nested BEGIN is attempted.
    Transaction ids advance between autocommit statements but stay fixed inside
    an explicit transaction, so two bounded scalar reads provide a safe probe.
    """
    first = con.execute("SELECT current_transaction_id()").fetchone()
    second = con.execute("SELECT current_transaction_id()").fetchone()
    assert first is not None and second is not None
    return first[0] == second[0]


def _activity_analytics_in_snapshot(
    con: duckdb.DuckDBPyConnection,
    filters: AnalyticsFilters,
    *,
    cursor_codec: AnalyticsCursorCodec | None = None,
) -> ActivityAnalytics:
    """Run every analytics statement inside the caller's current snapshot."""
    codec = cursor_codec or _DEFAULT_CURSOR_CODEC
    snapshot_at, cursor_snapshot = _cursor_snapshot_context(codec, filters)
    with _materialized_session_facts(con, filters, snapshot_at) as facts:
        return _activity_analytics_from_facts(
            con, filters, codec, snapshot_at, cursor_snapshot, facts
        )


@contextmanager
def _materialized_session_facts(
    con: duckdb.DuckDBPyConnection,
    filters: AnalyticsFilters,
    snapshot_at: datetime,
):
    """Yield one connection-scoped copy of the normalized request facts."""
    relation = f"analytics_session_facts_{secrets.token_hex(8)}"
    span_dates = _span_partition_dates(con, filters, snapshot_at)
    event_dates = _agent_event_partition_dates(con, filters, snapshot_at, span_dates)
    base_sql, params = _session_facts_sql(filters, snapshot_at, span_dates, event_dates)
    con.execute(
        f"CREATE TEMP TABLE {relation} AS {base_sql} SELECT * FROM filtered_sessions",
        params,
    )
    try:
        yield relation
    finally:
        try:
            con.execute(f"DROP TABLE IF EXISTS {relation}")
        except Exception:
            # Preserve the query failure that caused cleanup to run. A failed
            # transaction removes the request-scoped table when it rolls back.
            pass


def _span_partition_dates(
    con: duckdb.DuckDBPyConnection,
    filters: AnalyticsFilters,
    snapshot_at: datetime,
) -> tuple[str, ...]:
    """Return only span partitions that contain activity in this window.

    ``span_partition_activity`` is refreshed at bootstrap and ingestion, so a
    request retains long-running spans without inspecting old Parquet files.
    """
    rows = con.execute(
        """
        WITH bounds AS (
          SELECT CAST(? AS TIMESTAMPTZ)
                 - CAST(? AS INTEGER) * INTERVAL '1 day' AS cutoff
        )
        SELECT date
        FROM span_partition_activity, bounds
        WHERE latest_activity_at >= bounds.cutoff
        ORDER BY date
        """,
        [snapshot_at, filters.days],
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _agent_event_partition_dates(
    con: duckdb.DuckDBPyConnection,
    filters: AnalyticsFilters,
    snapshot_at: datetime,
    span_dates: tuple[str, ...],
) -> tuple[str, ...]:
    """Return agent-event partitions that can contribute to this window.

    Recent dates reconstruct event-only sessions. Adjacent dates preserve the
    established cross-midnight repo attribution for every included span. The
    inventory reads file paths only, so no global Parquet relation is bound.
    """
    available = {str(row[0]) for row in con.execute("""
            SELECT date
            FROM agent_event_partitions
            WHERE date IS NOT NULL AND date <> '_seed'
            """).fetchall()}
    cutoff_date = (snapshot_at - timedelta(days=filters.days + 1)).date()
    needed = {value for value in available if value >= cutoff_date.isoformat()}
    for value in span_dates:
        try:
            span_date = date.fromisoformat(value)
        except ValueError:
            continue
        needed.update(
            (span_date + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)
        )
    return tuple(sorted(available & needed))


def _activity_analytics_from_facts(
    con: duckdb.DuckDBPyConnection,
    filters: AnalyticsFilters,
    codec: AnalyticsCursorCodec,
    snapshot_at: datetime,
    cursor_snapshot: str | None,
    facts: str,
) -> ActivityAnalytics:
    snapshot_version = _snapshot_fingerprint(con, facts, snapshot_at)
    if cursor_snapshot is not None and cursor_snapshot != snapshot_version:
        raise AnalyticsSnapshotChangedError()
    aggregate = con.execute(f"""
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
          count(*) FILTER (WHERE project_key IS NOT NULL AND has_latency) AS latency_sessions,
          max(latest_activity_at) AS observed_at
        FROM {facts}
        """).fetchone()
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
    metadata = _aggregate_metadata(coverage, aggregate[12])
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
        metadata=metadata,
    )
    projects, projects_page = _project_breakdowns(
        con,
        facts,
        filters,
        project_metric,
        codec,
        snapshot_at,
        snapshot_version,
    )
    harnesses, harnesses_page = _dimension_breakdowns(
        con,
        facts,
        filters,
        "harness",
        codec,
        snapshot_at,
        snapshot_version,
    )
    hosts, hosts_page = _dimension_breakdowns(
        con,
        facts,
        filters,
        "host_id",
        codec,
        snapshot_at,
        snapshot_version,
    )
    models, models_page = _dimension_breakdowns(
        con,
        facts,
        filters,
        "model",
        codec,
        snapshot_at,
        snapshot_version,
    )
    return ActivityAnalytics(
        snapshot_at=snapshot_at,
        snapshot_version=snapshot_version,
        totals=totals,
        projects=projects,
        harnesses=harnesses,
        hosts=hosts,
        models=models,
        project_metric=project_metric,
        coverage=coverage,
        metadata=metadata,
        pagination=AnalyticsPagination(
            projects=projects_page,
            harnesses=harnesses_page,
            hosts=hosts_page,
            models=models_page,
        ),
    )


def _session_facts_sql(
    filters: AnalyticsFilters,
    snapshot_at: datetime,
    span_dates: tuple[str, ...],
    event_dates: tuple[str, ...],
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = [snapshot_at, filters.days]
    if event_dates:
        event_source = "\nUNION ALL BY NAME\n".join(
            "SELECT * FROM agent_events_for_date(?)" for _ in event_dates
        )
        params.extend(event_dates)
    else:
        event_source = """
          SELECT
            NULL::VARCHAR AS id,
            NULL::VARCHAR AS dedup_key,
            NULL::VARCHAR AS session_id,
            NULL::VARCHAR AS agent_id,
            NULL::TIMESTAMPTZ AS timestamp,
            NULL::VARCHAR AS repo_owner,
            NULL::VARCHAR AS repo_name,
            NULL::VARCHAR AS branch,
            NULL::VARCHAR AS date
          WHERE FALSE
        """
    if span_dates:
        span_source = "\nUNION ALL BY NAME\n".join(
            "SELECT * FROM spans_for_date(?)" for _ in span_dates
        )
        params.extend(span_dates)
    else:
        span_source = "SELECT * FROM spans_for_date('_seed')"
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
          SELECT CAST(? AS TIMESTAMPTZ)
                 - CAST(? AS INTEGER) * INTERVAL '1 day' AS cutoff
        ),
        bounded_agent_events AS (
          {event_source}
        ),
        {canonical_agent_events_cte(source="bounded_agent_events")},
        session_base AS (
          SELECT
            session_id,
            min(TRY_CAST(timestamp AS TIMESTAMPTZ)) AS started_at,
            max(TRY_CAST(timestamp AS TIMESTAMPTZ)) AS ended_at
          FROM canonical_agent_events, bounds
          WHERE session_id IS NOT NULL
            AND TRY_CAST(timestamp AS TIMESTAMPTZ) >= bounds.cutoff
          GROUP BY session_id
        ),
        bounded_spans AS (
          {span_source}
        ),
        span_session_days AS (
          SELECT DISTINCT session_id, date
          FROM bounded_spans
          WHERE session_id IS NOT NULL AND date IS NOT NULL
        ),
        span_agent_days AS (
          SELECT DISTINCT agent_id, date
          FROM bounded_spans
          WHERE agent_id IS NOT NULL AND date IS NOT NULL
        ),
        session_repos AS (
          SELECT session_id, date, repo_owner, repo_name
          FROM (
            SELECT sd.session_id,
                   sd.date,
                   ae.repo_owner,
                   ae.repo_name,
                   row_number() OVER (
                     PARTITION BY sd.session_id, sd.date
                     ORDER BY count(*) DESC, ae.repo_owner, ae.repo_name
                   ) AS rn
            FROM span_session_days sd
            JOIN canonical_agent_events ae
              ON ae.session_id = sd.session_id
             AND ae.date BETWEEN strftime(
                   TRY_CAST(sd.date AS DATE) - INTERVAL '1 day', '%Y-%m-%d'
                 )
                 AND strftime(
                   TRY_CAST(sd.date AS DATE) + INTERVAL '1 day', '%Y-%m-%d'
                 )
            WHERE ae.repo_owner IS NOT NULL
            GROUP BY sd.session_id, sd.date, ae.repo_owner, ae.repo_name
          )
          WHERE rn = 1
        ),
        agent_day_repos AS (
          SELECT agent_id, date, repo_owner, repo_name
          FROM (
            SELECT sad.agent_id,
                   sad.date,
                   ae.repo_owner,
                   ae.repo_name,
                   row_number() OVER (
                     PARTITION BY sad.agent_id, sad.date
                     ORDER BY count(*) DESC, ae.repo_owner, ae.repo_name
                   ) AS rn
            FROM span_agent_days sad
            JOIN canonical_agent_events ae
              ON ae.agent_id = sad.agent_id AND ae.date = sad.date
            WHERE ae.repo_owner IS NOT NULL
            GROUP BY sad.agent_id, sad.date, ae.repo_owner, ae.repo_name
          )
          WHERE rn = 1
        ),
        enriched_spans AS (
          SELECT
            s.* EXCLUDE (repo_owner, repo_name),
            COALESCE(s.repo_owner, sr.repo_owner, adr.repo_owner) AS repo_owner,
            COALESCE(s.repo_name, sr.repo_name, adr.repo_name) AS repo_name
          FROM bounded_spans s
          LEFT JOIN session_repos sr
            ON s.session_id = sr.session_id AND s.date = sr.date
          LEFT JOIN agent_day_repos adr
            ON s.agent_id = adr.agent_id AND s.date = adr.date
        ),
        span_sessions AS (
          SELECT
            session_id,
            min(start_time) AS started_at,
            max(
              COALESCE(GREATEST(end_time, start_time), end_time, start_time)
            ) AS latest_activity_at,
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
          FROM enriched_spans, bounds
          WHERE session_id IS NOT NULL
            AND COALESCE(GREATEST(end_time, start_time), end_time, start_time)
                >= bounds.cutoff
          GROUP BY session_id
        ),
        harness_base AS (
          SELECT hs.*
          FROM harness_sessions hs, bounds
          WHERE COALESCE(
              GREATEST(hs.updated_at, hs.ended_at, hs.started_at),
              hs.updated_at, hs.ended_at, hs.started_at
            ) >= bounds.cutoff
             OR EXISTS (
               SELECT 1 FROM span_sessions ss WHERE ss.session_id = hs.session_id
             )
             OR EXISTS (
               SELECT 1 FROM session_base s WHERE s.session_id = hs.session_id
             )
        ),
        session_facts AS (
          SELECT
            hs.session_id,
            COALESCE(hs.started_at, ss.started_at) AS started_at,
            COALESCE(
              GREATEST(
                hs.updated_at, hs.ended_at, hs.started_at,
                ss.latest_activity_at, sb.ended_at, sb.started_at
              ),
              hs.updated_at, hs.ended_at, hs.started_at,
              ss.latest_activity_at, sb.ended_at, sb.started_at
            ) AS latest_activity_at,
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
          LEFT JOIN session_base sb USING (session_id)

          UNION ALL

          SELECT
            ss.session_id, ss.started_at,
            COALESCE(
              GREATEST(ss.latest_activity_at, sb.ended_at, sb.started_at),
              ss.latest_activity_at, sb.ended_at, sb.started_at
            ) AS latest_activity_at,
            NULL AS host_id, ss.harness,
            ss.provider, ss.model, ss.project_key,
            ss.total_tokens, ss.cost_usd, ss.cache_read_tokens,
            ss.cache_write_tokens, ss.total_latency_ms, ss.has_tokens,
            ss.has_cost, ss.has_cache, ss.has_latency
          FROM span_sessions ss
          LEFT JOIN session_base sb USING (session_id)
          WHERE NOT EXISTS (
            SELECT 1 FROM harness_base hs WHERE hs.session_id = ss.session_id
          )

          UNION ALL

          SELECT
            s.session_id, s.started_at,
            COALESCE(
              GREATEST(s.ended_at, s.started_at), s.ended_at, s.started_at
            ) AS latest_activity_at,
            NULL AS host_id, NULL AS harness,
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
    facts: str,
    filters: AnalyticsFilters,
    metric: Literal["tokens", "sessions"],
    codec: AnalyticsCursorCodec,
    snapshot_at: datetime,
    snapshot_version: str,
) -> tuple[tuple[ProjectActivity, ...], PageMetadata]:
    order_column = "total_tokens" if metric == "tokens" else "session_count"
    cursor = _cursor_position(
        codec,
        filters.project_cursor,
        "projects",
        filters,
        metric,
        snapshot_at,
        snapshot_version,
    )
    having = ""
    query_params: list[Any] = []
    if cursor is not None:
        having = (
            f"HAVING ({order_column} < ? OR ({order_column} = ? AND project_key > ?))"
        )
        query_params.extend([cursor[0], cursor[0], cursor[1]])
    rows = con.execute(
        f"""
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
          list(DISTINCT host_id ORDER BY host_id) FILTER (WHERE host_id IS NOT NULL),
          count(*) FILTER (WHERE has_tokens) AS token_sessions,
          count(*) FILTER (WHERE has_cost) AS cost_sessions,
          count(*) FILTER (WHERE has_cache) AS cache_sessions,
          count(*) FILTER (WHERE has_latency) AS latency_sessions,
          max(latest_activity_at) AS observed_at
        FROM {facts}
        WHERE project_key IS NOT NULL
        GROUP BY project_key
        {having}
        ORDER BY {order_column} DESC, project_key
        LIMIT ?
        """,
        [*query_params, filters.limit + 1],
    ).fetchall()
    has_more = len(rows) > filters.limit
    page_rows = rows[: filters.limit]
    items = tuple(
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
            metadata=_aggregate_metadata(
                Coverage(
                    attributable_session_percent=100.0,
                    token_percent=_percent(int(row[10]), int(row[1])),
                    cost_percent=_percent(int(row[11]), int(row[1])),
                    cache_percent=_percent(int(row[12]), int(row[1])),
                    latency_percent=_percent(int(row[13]), int(row[1])),
                ),
                row[14],
            ),
        )
        for row in page_rows
    )
    next_cursor = None
    if has_more:
        row = page_rows[-1]
        next_cursor = _encode_cursor(
            codec,
            "projects",
            filters,
            metric,
            int(row[2]) if metric == "tokens" else int(row[1]),
            str(row[0]),
            snapshot_at,
            snapshot_version,
        )
    return items, PageMetadata(limit=filters.limit, next_cursor=next_cursor)


def _dimension_breakdowns(
    con: duckdb.DuckDBPyConnection,
    facts: str,
    filters: AnalyticsFilters,
    column: Literal["harness", "host_id", "model"],
    codec: AnalyticsCursorCodec,
    snapshot_at: datetime,
    snapshot_version: str,
) -> tuple[tuple[ActivityBreakdown, ...], PageMetadata]:
    dimension = {"harness": "harnesses", "host_id": "hosts", "model": "models"}[column]
    cursor_value = getattr(filters, f"{column.removesuffix('_id')}_cursor")
    cursor = _cursor_position(
        codec,
        cursor_value,
        dimension,
        filters,
        "sessions",
        snapshot_at,
        snapshot_version,
    )
    having = ""
    query_params: list[Any] = []
    if cursor is not None:
        having = "HAVING (session_count < ? OR (session_count = ? AND key > ?))"
        query_params.extend([cursor[0], cursor[0], cursor[1]])
    rows = con.execute(
        f"""
        SELECT
          {column} AS key,
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
          count(*) FILTER (WHERE project_key IS NOT NULL AND has_latency) AS latency_sessions,
          max(latest_activity_at) AS observed_at
        FROM {facts}
        WHERE {column} IS NOT NULL
        GROUP BY {column}
        {having}
        ORDER BY session_count DESC, key
        LIMIT ?
        """,
        [*query_params, filters.limit + 1],
    ).fetchall()
    has_more = len(rows) > filters.limit
    page_rows = rows[: filters.limit]
    items = tuple(
        ActivityBreakdown(
            key=str(row[0]),
            session_count=int(row[1]),
            total_tokens=int(row[2]),
            cost_usd=float(row[3]),
            cache_read_tokens=int(row[4]),
            cache_write_tokens=int(row[5]),
            total_latency_ms=float(row[6]),
            average_latency_ms=float(row[7]) if row[7] is not None else None,
            metadata=_aggregate_metadata(
                Coverage(
                    attributable_session_percent=_percent(int(row[8]), int(row[1])),
                    token_percent=_percent(int(row[9]), int(row[8])),
                    cost_percent=_percent(int(row[10]), int(row[8])),
                    cache_percent=_percent(int(row[11]), int(row[8])),
                    latency_percent=_percent(int(row[12]), int(row[8])),
                ),
                row[13],
            ),
        )
        for row in page_rows
    )
    next_cursor = None
    if has_more:
        row = page_rows[-1]
        next_cursor = _encode_cursor(
            codec,
            dimension,
            filters,
            "sessions",
            int(row[1]),
            str(row[0]),
            snapshot_at,
            snapshot_version,
        )
    return items, PageMetadata(limit=filters.limit, next_cursor=next_cursor)


def _filter_fingerprint(filters: AnalyticsFilters) -> str:
    values = {
        "days": filters.days,
        "host_id": filters.host_id,
        "harness": filters.harness,
        "provider": filters.provider,
        "model": filters.model,
        "project_key": filters.project_key,
    }
    body = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def _cursor_snapshot_context(
    codec: AnalyticsCursorCodec, filters: AnalyticsFilters
) -> tuple[datetime, str | None]:
    """Return one fixed cutoff and snapshot version shared by every cursor."""
    contexts: set[tuple[str, str]] = set()
    for dimension, cursor in (
        ("projects", filters.project_cursor),
        ("harnesses", filters.harness_cursor),
        ("hosts", filters.host_cursor),
        ("models", filters.model_cursor),
    ):
        if cursor is None:
            continue
        payload = codec.decode(cursor)
        expected = {
            "v": 2,
            "dimension": dimension,
            "filters": _filter_fingerprint(filters),
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ValueError(
                "analytics cursor does not match dimension, filters, or sort"
            )
        snapshot_at = payload.get("snapshot_at")
        snapshot_version = payload.get("snapshot_version")
        if not isinstance(snapshot_at, str) or not isinstance(snapshot_version, str):
            raise ValueError("invalid analytics cursor")
        contexts.add((snapshot_at, snapshot_version))
    if len(contexts) > 1:
        raise AnalyticsSnapshotChangedError()
    if not contexts:
        return datetime.now(timezone.utc), None
    snapshot_at_text, snapshot_version = contexts.pop()
    try:
        snapshot_at = datetime.fromisoformat(snapshot_at_text)
    except ValueError as exc:
        raise ValueError("invalid analytics cursor") from exc
    if snapshot_at.tzinfo is None:
        raise ValueError("invalid analytics cursor")
    return snapshot_at.astimezone(timezone.utc), snapshot_version


def _snapshot_fingerprint(
    con: duckdb.DuckDBPyConnection,
    facts: str,
    snapshot_at: datetime,
) -> str:
    """Bounded multiset fingerprint of every normalized fact used by analytics."""
    row = con.execute(f"""
        WITH fingerprint_rows AS (
          SELECT to_json(struct_pack(
            session_id := session_id,
            started_at := started_at,
            latest_activity_at := latest_activity_at,
            host_id := host_id,
            harness := harness,
            provider := provider,
            model := model,
            project_key := project_key,
            total_tokens := total_tokens,
            cost_usd := cost_usd,
            cache_read_tokens := cache_read_tokens,
            cache_write_tokens := cache_write_tokens,
            total_latency_ms := total_latency_ms,
            has_tokens := has_tokens,
            has_cost := has_cost,
            has_cache := has_cache,
            has_latency := has_latency
          )) AS body
          FROM {facts}
        )
        SELECT
          count(*),
          COALESCE(bit_xor(hash(body)), 0),
          COALESCE(sum(hash(body)), 0),
          COALESCE(bit_xor(hash('drover-analytics-snapshot-v1', body)), 0),
          COALESCE(sum(hash('drover-analytics-snapshot-v1', body)), 0)
        FROM fingerprint_rows
        """).fetchone()
    assert row is not None
    body = json.dumps(
        [
            snapshot_at.astimezone(timezone.utc).isoformat(),
            *[str(value) for value in row],
        ],
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(body).hexdigest()


def _aggregate_metadata(
    coverage: Coverage, observed_at: datetime | None
) -> AggregateMetadata:
    if observed_at is not None and observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    freshness: Literal["fresh", "stale", "unavailable"] = "unavailable"
    if observed_at is not None:
        age = (datetime.now(timezone.utc) - observed_at).total_seconds()
        freshness = "fresh" if age <= _FRESHNESS_SECONDS else "stale"
    return AggregateMetadata(
        source="drover_observed",
        observed_at=observed_at,
        freshness=freshness,
        coverage=coverage,
    )


def _encode_cursor(
    codec: AnalyticsCursorCodec,
    dimension: str,
    filters: AnalyticsFilters,
    sort: str,
    value: int,
    key: str,
    snapshot_at: datetime,
    snapshot_version: str,
) -> str:
    return codec.encode(
        {
            "v": 2,
            "dimension": dimension,
            "filters": _filter_fingerprint(filters),
            "sort": sort,
            "value": value,
            "key": key,
            "snapshot_at": snapshot_at.astimezone(timezone.utc).isoformat(),
            "snapshot_version": snapshot_version,
        }
    )


def _cursor_position(
    codec: AnalyticsCursorCodec,
    cursor: str | None,
    dimension: str,
    filters: AnalyticsFilters,
    sort: str,
    snapshot_at: datetime,
    snapshot_version: str,
) -> tuple[int, str] | None:
    if cursor is None:
        return None
    payload = codec.decode(cursor)
    expected = {
        "v": 2,
        "dimension": dimension,
        "filters": _filter_fingerprint(filters),
        "sort": sort,
        "snapshot_at": snapshot_at.astimezone(timezone.utc).isoformat(),
        "snapshot_version": snapshot_version,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("analytics cursor does not match dimension, filters, or sort")
    value = payload.get("value")
    key = payload.get("key")
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not isinstance(key, str)
    ):
        raise ValueError("invalid analytics cursor")
    return value, key


def _percent(numerator: int, denominator: int) -> float:
    return 100.0 * numerator / denominator if denominator else 0.0
