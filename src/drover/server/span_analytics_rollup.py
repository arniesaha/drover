"""Bounded span facts projection for foreground Analytics."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from drover.event_identity import canonical_agent_events_cte
from drover.server.db import open_duckdb_connection

if TYPE_CHECKING:
    from drover.server.analytics_maintenance import AnalyticalMaintenanceGate

log = logging.getLogger("drover.span_analytics_rollup")

DEFAULT_PARTITION_LIMIT = 1

_metrics_lock = threading.Lock()
_rolled_partitions_total = 0
_rolled_sessions_total = 0
_pending_partition_count = 0
_last_pass_seconds: float | None = None


@dataclass(frozen=True)
class SpanAnalyticsRollupReport:
    partitions: int
    sessions: int
    pending: int


def rollup_metrics() -> tuple[int, int, int, float | None]:
    """Return process-local rollup telemetry without opening the analytics store."""
    with _metrics_lock:
        return (
            _rolled_partitions_total,
            _rolled_sessions_total,
            _pending_partition_count,
            _last_pass_seconds,
        )


def reset_metrics_for_tests() -> None:
    global _rolled_partitions_total, _rolled_sessions_total
    global _pending_partition_count, _last_pass_seconds
    with _metrics_lock:
        _rolled_partitions_total = 0
        _rolled_sessions_total = 0
        _pending_partition_count = 0
        _last_pass_seconds = None


def _record_rollup_pass(
    report: SpanAnalyticsRollupReport, *, elapsed_seconds: float
) -> None:
    global _rolled_partitions_total, _rolled_sessions_total
    global _pending_partition_count, _last_pass_seconds
    with _metrics_lock:
        _rolled_partitions_total += report.partitions
        _rolled_sessions_total += report.sessions
        _pending_partition_count = report.pending
        _last_pass_seconds = elapsed_seconds


@dataclass(frozen=True)
class _PartitionAtom:
    session_id: str
    partition_date: str
    started_at: datetime
    latest_activity_at: datetime
    harness: str | None
    provider: str | None
    model: str | None
    project_key: str | None
    total_tokens: int | None
    cost_usd: float | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    total_latency_ms: float | None
    has_tokens: bool
    has_cost: bool
    has_cache: bool
    has_latency: bool
    source_row_count: int


def _pending_partitions(
    duckdb_path: Path, *, limit: int
) -> tuple[list[tuple[str, datetime]], int]:
    with open_duckdb_connection(duckdb_path, role="worker") as con:
        pending_sql = """
            SELECT activity.date, activity.latest_activity_at
            FROM span_partition_activity AS activity
            LEFT JOIN analytics_span_partition_watermarks AS watermark
              ON watermark.partition_date = activity.date
            WHERE watermark.source_activity_at IS NULL
               OR watermark.source_activity_at < activity.latest_activity_at
            ORDER BY activity.latest_activity_at, activity.date
            """
        pending_count = con.execute(f"SELECT count(*) FROM ({pending_sql})").fetchone()
        rows = con.execute(f"{pending_sql} LIMIT ?", [limit]).fetchall()
    return (
        [(str(row[0]), row[1]) for row in rows],
        int(pending_count[0]) if pending_count is not None else 0,
    )


def _load_partition_atoms(
    con: duckdb.DuckDBPyConnection, partition_date: str
) -> list[_PartitionAtom]:
    source_date = date.fromisoformat(partition_date)
    event_dates = [
        (source_date + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)
    ]
    available_event_dates = {str(row[0]) for row in con.execute("""
            SELECT date
            FROM agent_event_partitions
            WHERE date IS NOT NULL AND date <> '_seed'
            """).fetchall()}
    selected_event_dates = [
        event_date for event_date in event_dates if event_date in available_event_dates
    ]
    if selected_event_dates:
        event_source = "\nUNION ALL BY NAME\n".join("""
            SELECT id, dedup_key, session_id, agent_id, timestamp,
                   repo_owner, repo_name, branch, date
            FROM agent_events_for_date(?)
            """ for _ in selected_event_dates)
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
    rows = con.execute(
        f"""
        WITH bounded_spans AS (
          SELECT * FROM spans_for_date(?)
        ),
        bounded_agent_events AS (
          {event_source}
        ),
        {canonical_agent_events_cte(source="bounded_agent_events")},
        session_repos AS (
          SELECT session_id, repo_owner, repo_name
          FROM (
            SELECT spans.session_id,
                   events.repo_owner,
                   events.repo_name,
                   row_number() OVER (
                     PARTITION BY spans.session_id
                     ORDER BY count(*) DESC, events.repo_owner, events.repo_name
                   ) AS rn
            FROM (SELECT DISTINCT session_id FROM bounded_spans) AS spans
            JOIN canonical_agent_events AS events
              ON events.session_id = spans.session_id
            WHERE spans.session_id IS NOT NULL
              AND events.repo_owner IS NOT NULL
              AND events.repo_name IS NOT NULL
            GROUP BY spans.session_id, events.repo_owner, events.repo_name
          )
          WHERE rn = 1
        ),
        agent_day_repos AS (
          SELECT agent_id, repo_owner, repo_name
          FROM (
            SELECT spans.agent_id,
                   events.repo_owner,
                   events.repo_name,
                   row_number() OVER (
                     PARTITION BY spans.agent_id
                     ORDER BY count(*) DESC, events.repo_owner, events.repo_name
                   ) AS rn
            FROM (SELECT DISTINCT agent_id FROM bounded_spans) AS spans
            JOIN canonical_agent_events AS events
              ON events.agent_id = spans.agent_id
             AND events.date = ?
            WHERE spans.agent_id IS NOT NULL
              AND events.repo_owner IS NOT NULL
              AND events.repo_name IS NOT NULL
            GROUP BY spans.agent_id, events.repo_owner, events.repo_name
          )
          WHERE rn = 1
        ),
        enriched_spans AS (
          SELECT
            spans.* EXCLUDE (repo_owner, repo_name),
            COALESCE(spans.repo_owner, session_repos.repo_owner,
                     agent_day_repos.repo_owner) AS repo_owner,
            COALESCE(spans.repo_name, session_repos.repo_name,
                     agent_day_repos.repo_name) AS repo_name
          FROM bounded_spans AS spans
          LEFT JOIN session_repos USING (session_id)
          LEFT JOIN agent_day_repos USING (agent_id)
        )
        SELECT
          session_id,
          min(start_time) AS started_at,
          max(COALESCE(GREATEST(end_time, start_time), end_time, start_time))
            AS latest_activity_at,
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
          )::BIGINT AS total_tokens,
          sum(cost_usd) AS cost_usd,
          sum(cache_read_tokens)::BIGINT AS cache_read_tokens,
          sum(cache_write_tokens)::BIGINT AS cache_write_tokens,
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
        FROM enriched_spans
        WHERE session_id IS NOT NULL
        GROUP BY
          session_id,
          harness,
          llm_provider,
          COALESCE(llm_model, agent_model),
          repo_owner,
          repo_name
        ORDER BY session_id, harness, provider, model, project_key
        """,
        [partition_date, *selected_event_dates, partition_date],
    ).fetchall()
    return [
        _PartitionAtom(
            session_id=str(row[0]),
            partition_date=partition_date,
            started_at=row[1],
            latest_activity_at=row[2],
            harness=row[3],
            provider=row[4],
            model=row[5],
            project_key=row[6],
            total_tokens=row[7],
            cost_usd=row[8],
            cache_read_tokens=row[9],
            cache_write_tokens=row[10],
            total_latency_ms=row[11],
            has_tokens=bool(row[12]),
            has_cost=bool(row[13]),
            has_cache=bool(row[14]),
            has_latency=bool(row[15]),
            source_row_count=int(row[16]),
        )
        for row in rows
    ]


def _atom_id(atom: _PartitionAtom) -> str:
    fields = (
        atom.partition_date,
        atom.session_id,
        atom.harness or "",
        atom.provider or "",
        atom.model or "",
        atom.project_key or "",
    )
    return hashlib.sha256("\x1f".join(fields).encode()).hexdigest()


def _replace_partition_atoms(
    con: duckdb.DuckDBPyConnection,
    partition_date: str,
    atoms: list[_PartitionAtom],
) -> set[str]:
    prior_sessions = {
        str(row[0])
        for row in con.execute(
            """
            SELECT session_id
            FROM analytics_span_partition_totals
            WHERE partition_date = ?
            """,
            [partition_date],
        ).fetchall()
    }
    con.execute(
        "DELETE FROM analytics_span_partition_totals WHERE partition_date = ?",
        [partition_date],
    )
    for atom in atoms:
        con.execute(
            """
            INSERT INTO analytics_span_partition_totals (
              span_partition_total_id, session_id, partition_date, started_at,
              latest_activity_at, harness, provider, model, project_key,
              total_tokens, cost_usd, cache_read_tokens, cache_write_tokens,
              total_latency_ms, has_tokens, has_cost, has_cache, has_latency,
              source_row_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                _atom_id(atom),
                atom.session_id,
                atom.partition_date,
                atom.started_at,
                atom.latest_activity_at,
                atom.harness,
                atom.provider,
                atom.model,
                atom.project_key,
                atom.total_tokens,
                atom.cost_usd,
                atom.cache_read_tokens,
                atom.cache_write_tokens,
                atom.total_latency_ms,
                atom.has_tokens,
                atom.has_cost,
                atom.has_cache,
                atom.has_latency,
                atom.source_row_count,
            ],
        )
    return prior_sessions | {atom.session_id for atom in atoms}


def _rebuild_sessions(con: duckdb.DuckDBPyConnection, session_ids: set[str]) -> None:
    for session_id in session_ids:
        con.execute(
            "DELETE FROM analytics_span_sessions WHERE session_id = ?",
            [session_id],
        )
        con.execute(
            """
            INSERT INTO analytics_span_sessions (
              session_id, started_at, latest_activity_at, harness, provider,
              model, project_key, total_tokens, cost_usd, cache_read_tokens,
              cache_write_tokens, total_latency_ms, has_tokens, has_cost,
              has_cache, has_latency
            )
            WITH atoms AS (
              SELECT *
              FROM analytics_span_partition_totals
              WHERE session_id = ?
            ),
            project_counts AS (
              SELECT project_key, sum(source_row_count) AS row_count
              FROM atoms
              WHERE project_key IS NOT NULL
              GROUP BY project_key
            ),
            project_choice AS (
              SELECT project_key
              FROM project_counts
              ORDER BY row_count DESC, project_key
              LIMIT 1
            )
            SELECT
              ?,
              min(started_at),
              max(latest_activity_at),
              any_value(harness) FILTER (WHERE harness IS NOT NULL),
              any_value(provider) FILTER (WHERE provider IS NOT NULL),
              any_value(model) FILTER (WHERE model IS NOT NULL),
              (SELECT project_key FROM project_choice),
              sum(total_tokens)::BIGINT,
              sum(cost_usd),
              sum(cache_read_tokens)::BIGINT,
              sum(cache_write_tokens)::BIGINT,
              sum(total_latency_ms),
              bool_or(has_tokens),
              bool_or(has_cost),
              bool_or(has_cache),
              bool_or(has_latency)
            FROM atoms
            """,
            [session_id, session_id],
        )


def _upsert_watermark(
    con: duckdb.DuckDBPyConnection,
    partition_date: str,
    source_activity_at: datetime,
) -> None:
    con.execute(
        """
        INSERT INTO analytics_span_partition_watermarks
          (partition_date, source_activity_at)
        VALUES (?, ?)
        ON CONFLICT (partition_date) DO UPDATE SET
          source_activity_at = excluded.source_activity_at,
          rolled_at = now()
        """,
        [partition_date, source_activity_at],
    )


def rollup_pending_span_analytics(
    duckdb_path: Path,
    *,
    limit: int = DEFAULT_PARTITION_LIMIT,
    maintenance_gate: AnalyticalMaintenanceGate | None = None,
) -> SpanAnalyticsRollupReport:
    """Roll at most one changed source date into additive projection tables."""
    duckdb_path = Path(duckdb_path)
    if maintenance_gate is not None and not maintenance_gate.try_begin_maintenance():
        return SpanAnalyticsRollupReport(partitions=0, sessions=0, pending=0)
    try:
        pending, pending_count = _pending_partitions(duckdb_path, limit=limit)
        if not pending:
            return SpanAnalyticsRollupReport(partitions=0, sessions=0, pending=0)
        partition_date, source_activity_at = pending[0]
        with open_duckdb_connection(duckdb_path, role="worker") as con:
            atoms = _load_partition_atoms(con, partition_date)
            con.execute("BEGIN TRANSACTION")
            try:
                sessions = _replace_partition_atoms(con, partition_date, atoms)
                _rebuild_sessions(con, sessions)
                _upsert_watermark(con, partition_date, source_activity_at)
            except BaseException:
                con.execute("ROLLBACK")
                raise
            else:
                con.execute("COMMIT")
        return SpanAnalyticsRollupReport(
            partitions=1,
            sessions=len(sessions),
            pending=max(0, pending_count - 1),
        )
    finally:
        if maintenance_gate is not None:
            maintenance_gate.end_maintenance()


class SpanAnalyticsRollupWorker:
    """Periodically repair span projection facts after startup or ingest."""

    def __init__(
        self,
        *,
        duckdb_path: Path,
        maintenance_gate: AnalyticalMaintenanceGate | None = None,
        poll_interval_s: float = 60.0,
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self.maintenance_gate = maintenance_gate
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="drover-span-analytics-rollup", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception:  # noqa: BLE001 - later bounded pass repairs it.
                log.exception("span analytics rollup pass crashed")
            self._stop.wait(self.poll_interval_s)

    def drain_once(self) -> SpanAnalyticsRollupReport:
        started = time.monotonic()
        report = rollup_pending_span_analytics(
            self.duckdb_path, maintenance_gate=self.maintenance_gate
        )
        _record_rollup_pass(report, elapsed_seconds=time.monotonic() - started)
        return report
