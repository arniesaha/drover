"""Bounded analytics over Drover-observed normalized session facts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

import duckdb

from drover.event_identity import canonical_agent_events_cte
from drover.server.harness.usage import CACHE_INSIDE_INPUT_HARNESSES

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
class MetricSources:
    """Which sources could supply one metric, as a share of window sessions.

    ``usage_percent`` is ``None`` (never zero) when the ``session_usage``
    relation is not reachable on this connection; ``status`` says so.
    """

    usage_percent: float | None
    spans_percent: float
    status: Literal["ok", "unavailable"]


@dataclass(frozen=True)
class CoverageSources:
    tokens: MetricSources
    cache: MetricSources


@dataclass(frozen=True)
class Coverage:
    attributable_session_percent: float
    token_percent: float
    cost_percent: float
    cache_percent: float
    latency_percent: float
    sources: CoverageSources | None = None


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
class AnalyticsProjectionMetadata:
    """Completeness of compact historical span facts for this response."""

    status: Literal["ready", "catching_up"]
    completed_partition_count: int
    total_partition_count: int


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
    harness_attributed_session_count: int
    hosts: tuple[str, ...]
    host_attributed_session_count: int
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
    projection: AnalyticsProjectionMetadata
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


def _session_usage_available(con: duckdb.DuckDBPyConnection) -> bool:
    """True when ``session_usage`` resolves as a table or (temp) view here."""
    row = con.execute("""
        SELECT 1 FROM (
          SELECT table_name AS name FROM information_schema.tables
          UNION ALL
          SELECT view_name AS name FROM duckdb_views()
        )
        WHERE name = 'session_usage'
        LIMIT 1
        """).fetchone()
    return row is not None


def _span_projection_available(con: duckdb.DuckDBPyConnection) -> bool:
    """True once this store has the additive span projection relation."""
    row = con.execute("""
        SELECT 1
        FROM information_schema.tables
        WHERE table_name = 'analytics_span_partition_totals'
        LIMIT 1
        """).fetchone()
    return row is not None


def _activity_analytics_in_snapshot(
    con: duckdb.DuckDBPyConnection,
    filters: AnalyticsFilters,
    *,
    cursor_codec: AnalyticsCursorCodec | None = None,
) -> ActivityAnalytics:
    """Run every analytics statement inside the caller's current snapshot."""
    codec = cursor_codec or _DEFAULT_CURSOR_CODEC
    snapshot_at, cursor_snapshot = _cursor_snapshot_context(codec, filters)
    usage_available = _session_usage_available(con)
    projection = _analytics_projection_metadata(con)
    with _materialized_session_facts(
        con, filters, snapshot_at, usage_available=usage_available
    ) as facts:
        return _activity_analytics_from_facts(
            con,
            filters,
            codec,
            snapshot_at,
            cursor_snapshot,
            facts,
            usage_available=usage_available,
            projection=projection,
        )


@contextmanager
def _materialized_session_facts(
    con: duckdb.DuckDBPyConnection,
    filters: AnalyticsFilters,
    snapshot_at: datetime,
    *,
    usage_available: bool = False,
):
    """Yield one connection-scoped copy of the normalized request facts."""
    relation = f"analytics_session_facts_{secrets.token_hex(8)}"
    projection_available = _span_projection_available(con)
    span_dates = (
        _recent_span_partition_dates(con, filters, snapshot_at)
        if projection_available
        else _span_partition_dates(con, filters, snapshot_at)
    )
    event_dates = _agent_event_partition_dates(con, filters, snapshot_at, span_dates)
    base_sql, params = _session_facts_sql(
        filters,
        snapshot_at,
        span_dates,
        event_dates,
        usage_available=usage_available,
        historical_projection_start_date=(
            (snapshot_at - timedelta(days=filters.days)).date().isoformat()
            if projection_available
            else None
        ),
    )
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


def _analytics_projection_metadata(
    con: duckdb.DuckDBPyConnection,
) -> AnalyticsProjectionMetadata:
    """Count catch-up state from small metadata tables only.

    A pre-projection in-memory fixture remains a ready empty source.  A
    bootstrapped analytical store always has both relations, and counts a date
    complete only when its replacement watermark is at least as new as the
    source-activity index.
    """
    if not _span_projection_available(con):
        return AnalyticsProjectionMetadata("ready", 0, 0)
    try:
        row = con.execute("""
            SELECT
              count(*) AS total_partition_count,
              count(*) FILTER (
                WHERE watermark.source_activity_at >= activity.latest_activity_at
              ) AS completed_partition_count
            FROM span_partition_activity AS activity
            LEFT JOIN analytics_span_partition_watermarks AS watermark
              ON watermark.partition_date = activity.date
            """).fetchone()
    except duckdb.CatalogException:
        # Tests and old callers can provide a projection relation in isolation;
        # only bootstrapped stores advertise a meaningful catch-up count.
        return AnalyticsProjectionMetadata("ready", 0, 0)
    assert row is not None
    total = int(row[0] or 0)
    completed = int(row[1] or 0)
    return AnalyticsProjectionMetadata(
        "ready" if completed == total else "catching_up",
        completed,
        total,
    )


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


def _recent_span_partition_dates(
    con: duckdb.DuckDBPyConnection,
    filters: AnalyticsFilters,
    snapshot_at: datetime,
) -> tuple[str, ...]:
    """Return physical raw partitions in the request's recent calendar range.

    Historical dates are intentionally absent here: their compact projection
    rows are selected directly by ``_session_facts_sql``.  Intersecting a
    generated, bounded date range with the partition inventory avoids calling
    a DuckDB date macro for a physical directory that does not exist.
    """
    cutoff_date = (snapshot_at - timedelta(days=filters.days)).date()
    current_date = snapshot_at.date()
    candidates = {
        (cutoff_date + timedelta(days=offset)).isoformat()
        for offset in range((current_date - cutoff_date).days + 1)
    }
    available = {str(row[0]) for row in con.execute("""
            SELECT date
            FROM span_partitions
            WHERE date IS NOT NULL AND date <> '_seed'
            """).fetchall()}
    return tuple(sorted(candidates & available))


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
    *,
    usage_available: bool = False,
    projection: AnalyticsProjectionMetadata,
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
          max(latest_activity_at) AS observed_at,
          count(*) FILTER (WHERE usage_has_tokens) AS usage_token_sessions,
          count(*) FILTER (WHERE span_has_tokens) AS span_token_sessions,
          count(*) FILTER (WHERE usage_has_cache) AS usage_cache_sessions,
          count(*) FILTER (WHERE span_has_cache) AS span_cache_sessions,
          count(*) FILTER (WHERE project_key IS NOT NULL AND usage_has_tokens)
            AS attributed_usage_token_sessions,
          count(*) FILTER (WHERE project_key IS NOT NULL AND span_has_tokens)
            AS attributed_span_token_sessions
        FROM {facts}
        """).fetchone()
    assert aggregate is not None
    session_count = int(aggregate[0] or 0)
    attributable_sessions = int(aggregate[7] or 0)
    status: Literal["ok", "unavailable"] = "ok" if usage_available else "unavailable"
    usage_token_pct = (
        _percent(int(aggregate[13] or 0), session_count) if usage_available else None
    )
    span_token_pct = _percent(int(aggregate[14] or 0), session_count)
    usage_cache_pct = (
        _percent(int(aggregate[15] or 0), session_count) if usage_available else None
    )
    span_cache_pct = _percent(int(aggregate[16] or 0), session_count)
    coverage = Coverage(
        attributable_session_percent=_percent(attributable_sessions, session_count),
        token_percent=_percent(int(aggregate[8] or 0), session_count),
        cost_percent=_percent(int(aggregate[9] or 0), session_count),
        cache_percent=_percent(int(aggregate[10] or 0), session_count),
        latency_percent=_percent(int(aggregate[11] or 0), session_count),
        sources=CoverageSources(
            tokens=MetricSources(usage_token_pct, span_token_pct, status),
            cache=MetricSources(usage_cache_pct, span_cache_pct, status),
        ),
    )
    metadata = _aggregate_metadata(coverage, aggregate[12])
    # Spec, Track 3: rank projects by tokens only when one source alone covers
    # enough sessions to trust. A union of two thin sources is not that.
    #
    # The gate counts attributed sessions only, while the `sources` block above
    # counts every session in the window. The two answer different questions:
    # `sources` is provenance ("where did these numbers come from"), and the
    # breakdowns it describes include unattributed sessions. Ranking is about
    # the projects list, which by construction holds only attributed sessions --
    # gating it on window-wide coverage would order projects by a column that
    # is zero for every one of them whenever the token-bearing sessions carry
    # no repository.
    attributed_usage_token_pct = (
        _percent(int(aggregate[17] or 0), session_count) if usage_available else None
    )
    attributed_span_token_pct = _percent(int(aggregate[18] or 0), session_count)
    project_metric: Literal["tokens", "sessions"] = (
        "tokens"
        if max(attributed_usage_token_pct or 0.0, attributed_span_token_pct)
        >= _TOKEN_COVERAGE_THRESHOLD
        else "sessions"
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
        projection=projection,
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
    *,
    usage_available: bool = False,
    historical_projection_start_date: str | None = None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = [snapshot_at, filters.days]
    if event_dates:
        # The dedup window below (canonical_agent_events_cte) sorts every
        # column it is fed, and raw_data is a multi-KB JSON blob per row
        # (measured ~40% of this statement's runtime on a ~2.15M-event
        # 30-day window). The only downstream consumer of raw_data is
        # session_base's is_claude_mem_observer check, which only needs a
        # single resolved cwd string. Pre-extract that cwd here, before the
        # window, and carry a tiny synthetic raw_data instead of the
        # original blob.
        per_date_source = """
          SELECT id, dedup_key, session_id, agent_id, timestamp,
                 repo_owner, repo_name, branch, date,
                 CASE WHEN json_valid(raw_data) THEN to_json(struct_pack(cwd :=
                   COALESCE(
                     NULLIF(trim(json_extract_string(raw_data, '$.cwd')), ''),
                     NULLIF(trim(json_extract_string(
                       raw_data, '$.currentWorkingDirectory'
                     )), ''),
                     NULLIF(trim(json_extract_string(
                       raw_data, '$.working_directory'
                     )), ''),
                     NULLIF(trim(json_extract_string(raw_data, '$.workspaceDir')), ''),
                     ''
                   )))::VARCHAR
                 ELSE NULL END AS raw_data
          FROM agent_events_for_date(?)
        """
        event_source = "\nUNION ALL BY NAME\n".join(
            per_date_source for _ in event_dates
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
            NULL::VARCHAR AS raw_data,
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
    if historical_projection_start_date is not None:
        params.append(historical_projection_start_date)
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
    usage_source = (
        "session_usage"
        if usage_available
        else """(
          SELECT NULL::VARCHAR AS session_id, NULL::BIGINT AS input_tokens,
                 NULL::BIGINT AS output_tokens, NULL::BIGINT AS cache_read_tokens,
                 NULL::BIGINT AS cache_write_tokens, NULL::VARCHAR AS harness
          WHERE FALSE
        )"""
    )
    cache_inside_input_harnesses_sql = ", ".join(
        "'" + name + "'" for name in sorted(CACHE_INSIDE_INPUT_HARNESSES)
    )
    if historical_projection_start_date is None:
        span_session_ctes = """
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
        )
        """
    else:
        # Old source partitions are represented by their compact atoms.  The
        # raw relation contains only recent calendar partitions, so this path
        # cannot fan out across historic Parquet even for a long-running span.
        span_session_ctes = """
        current_span_atoms AS (
          SELECT
            session_id,
            min(start_time) AS started_at,
            max(
              COALESCE(GREATEST(end_time, start_time), end_time, start_time)
            ) AS latest_activity_at,
            harness,
            llm_provider AS provider,
            COALESCE(llm_model, agent_model) AS model,
            CASE
              WHEN repo_owner IS NOT NULL AND repo_name IS NOT NULL
                THEN repo_owner || '/' || repo_name
              ELSE NULL
            END AS project_key,
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
            bool_or(duration_ms IS NOT NULL) AS has_latency,
            count(*)::BIGINT AS source_row_count
          FROM enriched_spans, bounds
          WHERE session_id IS NOT NULL
            AND COALESCE(GREATEST(end_time, start_time), end_time, start_time)
                >= bounds.cutoff
          GROUP BY
            session_id,
            harness,
            llm_provider,
            COALESCE(llm_model, agent_model),
            repo_owner,
            repo_name
        ),
        historical_span_atoms AS (
          SELECT
            session_id,
            started_at,
            latest_activity_at,
            harness,
            provider,
            model,
            project_key,
            total_tokens,
            cost_usd,
            cache_read_tokens,
            cache_write_tokens,
            total_latency_ms,
            has_tokens,
            has_cost,
            has_cache,
            has_latency,
            source_row_count
          FROM analytics_span_partition_totals, bounds
          WHERE partition_date < ?
            AND latest_activity_at >= bounds.cutoff
        ),
        span_atoms AS (
          SELECT * FROM current_span_atoms
          UNION ALL BY NAME
          SELECT * FROM historical_span_atoms
        ),
        project_counts AS (
          SELECT session_id, project_key, sum(source_row_count) AS row_count
          FROM span_atoms
          WHERE project_key IS NOT NULL
          GROUP BY session_id, project_key
        ),
        project_choices AS (
          SELECT session_id, project_key
          FROM (
            SELECT
              session_id,
              project_key,
              row_number() OVER (
                PARTITION BY session_id
                ORDER BY row_count DESC, project_key
              ) AS rn
            FROM project_counts
          )
          WHERE rn = 1
        ),
        span_session_values AS (
          SELECT
            session_id,
            min(started_at) AS started_at,
            max(latest_activity_at) AS latest_activity_at,
            any_value(harness) FILTER (WHERE harness IS NOT NULL) AS harness,
            any_value(provider) FILTER (WHERE provider IS NOT NULL) AS provider,
            any_value(model) FILTER (WHERE model IS NOT NULL) AS model,
            sum(total_tokens) AS total_tokens,
            sum(cost_usd) AS cost_usd,
            sum(cache_read_tokens) AS cache_read_tokens,
            sum(cache_write_tokens) AS cache_write_tokens,
            sum(total_latency_ms) AS total_latency_ms,
            bool_or(has_tokens) AS has_tokens,
            bool_or(has_cost) AS has_cost,
            bool_or(has_cache) AS has_cache,
            bool_or(has_latency) AS has_latency
          FROM span_atoms
          GROUP BY session_id
        ),
        span_sessions AS (
          SELECT values.*, choices.project_key
          FROM span_session_values AS values
          LEFT JOIN project_choices AS choices USING (session_id)
        )
        """
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
            max(TRY_CAST(timestamp AS TIMESTAMPTZ)) AS ended_at,
            mode(repo_owner || '/' || repo_name) FILTER (
              WHERE repo_owner IS NOT NULL AND repo_name IS NOT NULL
            ) AS project_key,
            bool_or(
              CASE
                WHEN json_valid(raw_data) THEN ends_with(
                  rtrim(COALESCE(
                    NULLIF(trim(json_extract_string(raw_data, '$.cwd')), ''),
                    ''
                  ), '/'),
                  '/claude/mem/observer/sessions'
                )
                ELSE FALSE
              END
            ) AS is_claude_mem_observer
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
        {span_session_ctes},
        usage_sessions AS (
          SELECT
            session_id,
            CASE
              WHEN input_tokens IS NOT NULL OR output_tokens IS NOT NULL
                THEN
                  CASE
                    WHEN harness IN ({cache_inside_input_harnesses_sql})
                      THEN COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)
                    ELSE
                      COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)
                        + COALESCE(cache_read_tokens, 0)
                        + COALESCE(cache_write_tokens, 0)
                  END
              ELSE NULL
            END AS total_tokens,
            cache_read_tokens,
            cache_write_tokens,
            (input_tokens IS NOT NULL OR output_tokens IS NOT NULL) AS has_tokens,
            (cache_read_tokens IS NOT NULL OR cache_write_tokens IS NOT NULL)
              AS has_cache
          FROM {usage_source}
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
        session_ids AS (
          -- Each source is already unique by session_id. Build the small key
          -- set once, then join the three sources once. The previous shape
          -- expressed this as three UNION ALL branches with correlated
          -- NOT EXISTS de-duplication; on the production 30-day window that
          -- final merge consumed 23.7 of 26.8 seconds despite receiving only
          -- 117, 153 and 6,715 input rows (#260).
          SELECT session_id FROM harness_base
          UNION
          SELECT session_id FROM span_sessions
          UNION
          SELECT session_id FROM session_base
          WHERE NOT is_claude_mem_observer
        ),
        session_facts AS (
          SELECT
            ids.session_id,
            CASE
              -- Preserve the historical precedence exactly: an event-only
              -- started_at does not fill a registered session whose harness
              -- and span timestamps are both absent.
              WHEN hs.session_id IS NOT NULL
                THEN COALESCE(hs.started_at, ss.started_at)
              ELSE COALESCE(ss.started_at, sb.started_at)
            END AS started_at,
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
              sb.project_key,
              CASE
                WHEN hs.repo_owner IS NOT NULL AND hs.repo_name IS NOT NULL
                  THEN hs.repo_owner || '/' || hs.repo_name
                ELSE NULL
              END
            ) AS project_key,
            COALESCE(us.total_tokens, ss.total_tokens) AS total_tokens,
            ss.cost_usd,
            COALESCE(us.cache_read_tokens, ss.cache_read_tokens) AS cache_read_tokens,
            COALESCE(us.cache_write_tokens, ss.cache_write_tokens) AS cache_write_tokens,
            ss.total_latency_ms,
            (COALESCE(us.has_tokens, FALSE) OR COALESCE(ss.has_tokens, FALSE))
              AS has_tokens,
            COALESCE(ss.has_cost, FALSE) AS has_cost,
            (COALESCE(us.has_cache, FALSE) OR COALESCE(ss.has_cache, FALSE))
              AS has_cache,
            COALESCE(ss.has_latency, FALSE) AS has_latency,
            COALESCE(us.has_tokens, FALSE) AS usage_has_tokens,
            COALESCE(ss.has_tokens, FALSE) AS span_has_tokens,
            COALESCE(us.has_cache, FALSE) AS usage_has_cache,
            COALESCE(ss.has_cache, FALSE) AS span_has_cache
          FROM session_ids ids
          LEFT JOIN harness_base hs USING (session_id)
          LEFT JOIN span_sessions ss USING (session_id)
          LEFT JOIN session_base sb USING (session_id)
          LEFT JOIN usage_sessions us USING (session_id)
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
          count(*) FILTER (WHERE harness IS NOT NULL),
          list(DISTINCT host_id ORDER BY host_id) FILTER (WHERE host_id IS NOT NULL),
          count(*) FILTER (WHERE host_id IS NOT NULL),
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
            harness_attributed_session_count=int(row[9]),
            hosts=tuple(row[10] or ()),
            host_attributed_session_count=int(row[11]),
            metadata=_aggregate_metadata(
                Coverage(
                    attributable_session_percent=100.0,
                    token_percent=_percent(int(row[12]), int(row[1])),
                    cost_percent=_percent(int(row[13]), int(row[1])),
                    cache_percent=_percent(int(row[14]), int(row[1])),
                    latency_percent=_percent(int(row[15]), int(row[1])),
                ),
                row[16],
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
                    token_percent=_percent(int(row[9]), int(row[1])),
                    cost_percent=_percent(int(row[10]), int(row[1])),
                    cache_percent=_percent(int(row[11]), int(row[1])),
                    latency_percent=_percent(int(row[12]), int(row[1])),
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
            has_latency := has_latency,
            usage_has_tokens := usage_has_tokens,
            span_has_tokens := span_has_tokens,
            usage_has_cache := usage_has_cache,
            span_has_cache := span_has_cache
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
