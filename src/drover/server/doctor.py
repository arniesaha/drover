"""Lakehouse self-audit.

Counts rows by partition and compares to the per-host
``incoming/<host>/.processed/`` manifest. Drift > 1% raises a warning.
The intent is a nightly "is anything silently rotting?" check.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import Optional

import duckdb

from drover.attribution import (
    GENERAL_WORKSPACE_ACTIVITY_TYPE,
    configured_general_workspace_roots,
)
from drover.event_identity import (
    audit_agent_event_identity,
    canonical_agent_events_cte,
    scan_agent_events_once,
)
from drover.server.db import open_duckdb_connection
from drover.server.summarizer.retry import classify_summarize_error
from drover.session_audit import audit_session_consistency

log = logging.getLogger("drover.doctor")


RUNTIME_KEY_RELATIONS = (
    "agent_events",
    "spans",
    "tasks",
    "session_summaries",
    "summarize_jobs",
    "embed_jobs",
    "session_embeddings",
    "span_embed_jobs",
    "span_embeddings",
)

_CWD_SQL = """COALESCE(
    CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.cwd') END,
    CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.currentWorkingDirectory') END,
    CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.working_directory') END,
    CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.workspaceDir') END
)"""


def _sql_string_list(values: list[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def _general_workspace_sql(cwd_expr: str = "cwd") -> str:
    roots = _sql_string_list(sorted(configured_general_workspace_roots())) or "NULL"
    return f"""(
        (json_valid(raw_data)
         AND json_extract_string(raw_data, '$._nexus_activity_type') = ?)
        OR {cwd_expr} IN ({roots})
    )"""


def _safe_count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    try:
        row = con.execute(sql).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except duckdb.Error:
        return 0


def _per_host_processed(incoming_dir: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    if not incoming_dir.exists():
        return out
    for host_dir in sorted(p for p in incoming_dir.iterdir() if p.is_dir()):
        processed = host_dir / ".processed"
        if processed.exists():
            n = sum(1 for _ in processed.glob("*.jsonl"))
            out[host_dir.name] = n
    return out


def _safe_rows(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: object | None = None,
    *,
    warnings: list[str] | None = None,
    label: str | None = None,
) -> list:
    try:
        if params is None:
            return list(con.execute(sql).fetchall())
        return list(con.execute(sql, params).fetchall())
    except duckdb.Error as e:
        if warnings is not None and label:
            warnings.append(f"{label} query failed: {e}")
        return []


def _relation_count(con: duckdb.DuckDBPyConnection, name: str) -> int | None:
    try:
        row = con.execute(f"SELECT count(*) FROM {name}").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except duckdb.Error:
        return None


def _relation_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    try:
        row = con.execute(
            """
            SELECT count(*)
            FROM duckdb_tables()
            WHERE table_name = ?
            UNION ALL
            SELECT count(*)
            FROM duckdb_views()
            WHERE view_name = ?
            """,
            [name, name],
        ).fetchall()
        return any(int(count or 0) > 0 for (count,) in row)
    except duckdb.Error:
        return False


def _status_counts(con: duckdb.DuckDBPyConnection, table: str) -> dict[str, int]:
    rows = _safe_rows(
        con,
        f"SELECT COALESCE(status, '<null>') AS status, count(*) FROM {table} GROUP BY 1 ORDER BY 1",
    )
    return {str(status): int(n) for status, n in rows}


_SECRET_ERROR_PATTERNS = (
    re.compile(r"(?i)(token|api[_-]?key|authorization|bearer)=([^\s;,]+)"),
    re.compile(r"(?i)(sk-ant-[A-Za-z0-9_-]+)"),
)


def _redact_error_summary(message: object, *, max_len: int = 160) -> str:
    text = " ".join(str(message or "").split())
    for pattern in _SECRET_ERROR_PATTERNS:
        text = pattern.sub(
            lambda m: (
                f"{m.group(1)}=<redacted>" if len(m.groups()) > 1 else "<redacted>"
            ),
            text,
        )
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _summarize_job_backend_health(
    con: duckdb.DuckDBPyConnection, counts: dict[str, int]
) -> dict:
    pending = int(counts.get("pending", 0) or 0)
    running = int(counts.get("running", 0) or 0)
    errored = int(counts.get("errored", 0) or 0)
    categories: dict[str, int] = {}
    retryable = 0
    error_rows = _safe_rows(
        con,
        "SELECT last_error FROM summarize_jobs WHERE status='errored' AND last_error IS NOT NULL",
    )
    for (last_error,) in error_rows:
        classification = classify_summarize_error(last_error)
        category = str(classification.get("category") or "unknown")
        categories[category] = categories.get(category, 0) + 1
        if classification.get("retryable"):
            retryable += 1
    non_retryable = max(len(error_rows) - retryable, 0)
    if pending or running:
        state = "backlog_with_retryable_errors" if retryable else "backlog"
    elif errored:
        state = "retryable_errors" if retryable else "errors"
    else:
        state = "idle"
    return {
        "state": state,
        "pending": pending,
        "running": running,
        "errored": errored,
        "retryable_errors": retryable,
        "non_retryable_errors": non_retryable,
        "error_categories": categories,
    }


def _recent_job_errors(
    con: duckdb.DuckDBPyConnection, table: str, *, limit: int = 5
) -> list[dict]:
    rows = _safe_rows(
        con,
        f"""
        SELECT session_id, status, attempts, last_error, COALESCE(updated_at, enqueued_at) AS ts
        FROM {table}
        WHERE COALESCE(status, '') <> 'done'
          AND last_error IS NOT NULL
        ORDER BY COALESCE(updated_at, enqueued_at) DESC NULLS LAST
        LIMIT ?
        """,
        [limit],
    )
    errors = []
    for session_id, status, attempts, last_error, ts in rows:
        classification = classify_summarize_error(last_error)
        errors.append(
            {
                "session_id": session_id,
                "status": status,
                "attempts": attempts,
                "last_error": last_error,
                "last_error_summary": _redact_error_summary(last_error),
                "error_category": classification["category"],
                "retryable": classification["retryable"],
                "timestamp": str(ts) if ts is not None else None,
            }
        )
    return errors


def _repo_attribution_for_window(
    con: duckdb.DuckDBPyConnection,
    *,
    hours: int,
    warnings: list[str],
) -> dict[str, dict]:
    """Return per-agent attribution percentages for a lookback window."""
    recent_days = max(2, int(ceil(hours / 24)) + 2)
    rows = _safe_rows(
        con,
        f"""
        WITH {canonical_agent_events_cte()},
        classified AS (
            SELECT agent_id,
                   repo_owner,
                   repo_name,
                   CASE
                     WHEN {_general_workspace_sql(_CWD_SQL)} THEN true
                     ELSE false
                   END AS is_general_workspace
            FROM canonical_agent_events
            WHERE date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
              AND TRY_CAST(timestamp AS TIMESTAMP) >= now() - ? * INTERVAL '1 hour'
              AND agent_id IS NOT NULL
        )
        SELECT agent_id,
               count(*) AS total,
               count(*) FILTER (WHERE repo_owner IS NOT NULL AND repo_name IS NOT NULL) AS attributed,
               count(*) FILTER (
                 WHERE (repo_owner IS NULL OR repo_name IS NULL) AND is_general_workspace
               ) AS general_workspace
        FROM classified
        GROUP BY agent_id
        ORDER BY agent_id
        """,
        [GENERAL_WORKSPACE_ACTIVITY_TYPE, recent_days, hours],
        warnings=warnings,
        label=f"repo attribution {hours}h",
    )
    return {
        str(agent_id): {
            "total": int(total),
            "attributed": int(attributed),
            "general_workspace": int(general_workspace),
            "project_total": max(int(total) - int(general_workspace), 0),
            "percent": (
                round(
                    (int(attributed) / max(int(total) - int(general_workspace), 0))
                    * 100,
                    1,
                )
                if max(int(total) - int(general_workspace), 0)
                else 100.0
            ),
        }
        for agent_id, total, attributed, general_workspace in rows
    }


def _top_unattributed_cwds_for_window(
    con: duckdb.DuckDBPyConnection,
    *,
    hours: int,
    warnings: list[str],
    limit: int = 10,
) -> list[dict]:
    """Return top cwd/workspaceDir samples for unattributed events."""
    recent_days = max(2, int(ceil(hours / 24)) + 2)
    rows = _safe_rows(
        con,
        f"""
        WITH {canonical_agent_events_cte()},
        unattributed AS (
            SELECT agent_id,
                   COALESCE(
                       CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.cwd') END,
                       CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.currentWorkingDirectory') END,
                       CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.working_directory') END,
                       CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.workspaceDir') END,
                       '<missing>'
                   ) AS cwd,
                   CASE
                     WHEN {_general_workspace_sql(_CWD_SQL)} THEN true
                     ELSE false
                   END AS is_general_workspace
            FROM canonical_agent_events
            WHERE date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
              AND TRY_CAST(timestamp AS TIMESTAMP) >= now() - ? * INTERVAL '1 hour'
              AND agent_id IS NOT NULL
              AND (repo_owner IS NULL OR repo_name IS NULL)
        )
        SELECT agent_id, cwd, count(*) AS n
        FROM unattributed
        WHERE NOT is_general_workspace
        GROUP BY agent_id, cwd
        ORDER BY n DESC, agent_id, cwd
        LIMIT ?
        """,
        [GENERAL_WORKSPACE_ACTIVITY_TYPE, recent_days, hours, limit],
        warnings=warnings,
        label=f"top unattributed cwd {hours}h",
    )
    return [
        {"agent_id": str(agent_id), "cwd": str(cwd), "count": int(n)}
        for agent_id, cwd, n in rows
    ]


def _general_workspace_cwds_for_window(
    con: duckdb.DuckDBPyConnection,
    *,
    hours: int,
    warnings: list[str],
    limit: int = 10,
) -> list[dict]:
    """Return top known non-project cwd samples excluded from attribution SLOs."""
    recent_days = max(2, int(ceil(hours / 24)) + 2)
    rows = _safe_rows(
        con,
        f"""
        WITH {canonical_agent_events_cte()},
        classified AS (
            SELECT agent_id,
                   COALESCE(
                       CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.cwd') END,
                       CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.currentWorkingDirectory') END,
                       CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.working_directory') END,
                       CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.workspaceDir') END,
                       '<missing>'
                   ) AS cwd,
                   CASE
                     WHEN {_general_workspace_sql(_CWD_SQL)} THEN true
                     ELSE false
                   END AS is_general_workspace
            FROM canonical_agent_events
            WHERE date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
              AND TRY_CAST(timestamp AS TIMESTAMP) >= now() - ? * INTERVAL '1 hour'
              AND agent_id IS NOT NULL
              AND (repo_owner IS NULL OR repo_name IS NULL)
        )
        SELECT agent_id, cwd, count(*) AS n
        FROM classified
        WHERE is_general_workspace
        GROUP BY agent_id, cwd
        ORDER BY n DESC, agent_id, cwd
        LIMIT ?
        """,
        [GENERAL_WORKSPACE_ACTIVITY_TYPE, recent_days, hours, limit],
        warnings=warnings,
        label=f"general workspace cwd {hours}h",
    )
    return [
        {"agent_id": str(agent_id), "cwd": str(cwd), "count": int(n)}
        for agent_id, cwd, n in rows
    ]


def _claude_attribution_gap_categories_for_window(
    con: duckdb.DuckDBPyConnection,
    *,
    hours: int,
    warnings: list[str],
) -> dict[str, dict]:
    """Classify current Claude repo-attribution gaps without inventing repos."""
    recent_days = max(2, int(ceil(hours / 24)) + 2)
    rows = _safe_rows(
        con,
        f"""
        WITH {canonical_agent_events_cte()},
        classified AS (
            SELECT agent_id,
                   COALESCE({_CWD_SQL}, '<missing>') AS cwd,
                   CASE
                     WHEN {_general_workspace_sql(_CWD_SQL)} THEN 'general_context_activity'
                     WHEN NOT json_valid(raw_data) THEN 'parser_collector_drift'
                     WHEN {_CWD_SQL} IS NULL THEN 'missing_producer_metadata'
                     ELSE 'genuine_unknown'
                   END AS category,
                   count(*) AS n
            FROM canonical_agent_events
            WHERE date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
              AND TRY_CAST(timestamp AS TIMESTAMP) >= now() - ? * INTERVAL '1 hour'
              AND lower(agent_id) LIKE '%claude%'
              AND (repo_owner IS NULL OR repo_name IS NULL)
            GROUP BY 1, 2, 3
        ),
        ranked AS (
            SELECT *,
                   row_number() OVER (
                       PARTITION BY agent_id, category ORDER BY n DESC, cwd
                   ) AS rn
            FROM classified
        )
        SELECT agent_id, category, sum(n) AS total,
               list(struct_pack(cwd := cwd, count := n) ORDER BY n DESC, cwd) FILTER (WHERE rn <= 5) AS samples
        FROM ranked
        GROUP BY agent_id, category
        ORDER BY agent_id, category
        """,
        [GENERAL_WORKSPACE_ACTIVITY_TYPE, recent_days, hours],
        warnings=warnings,
        label=f"Claude attribution gap categories {hours}h",
    )
    out: dict[str, dict] = {}
    for agent_id, category, total, samples in rows:
        agent = out.setdefault(str(agent_id), {})
        agent[str(category)] = {
            "count": int(total),
            "samples": [
                {"cwd": str(sample.get("cwd")), "count": int(sample.get("count", 0))}
                for sample in (samples or [])
            ],
        }
    return out


def _span_health_for_window(
    con: duckdb.DuckDBPyConnection,
    *,
    days: int,
    warnings: list[str],
) -> dict:
    """Return partition-pruned span freshness/cost health.

    Runtime health must never execute broad historical ``spans`` scans. The
    ``date`` predicate is intentionally on the hive partition column so DuckDB
    can prune old parquet files before casting timestamps or touching enriched
    attribution fields.
    """
    rows = _safe_rows(
        con,
        """
        SELECT count(*) AS recent_count,
               MIN(start_time) AS earliest_start,
               MAX(start_time) AS latest_start,
               SUM(cost_usd) AS cost_usd,
               COUNT(DISTINCT date) AS partition_days
        FROM spans
        WHERE date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
          AND date <> '_seed'
        """,
        [days],
        warnings=warnings,
        label="span health",
    )
    if not rows:
        return {
            "status": "missing",
            "recent_count": None,
            "latest_start": None,
            "earliest_start": None,
            "cost_usd": None,
            "partition_days": None,
            "window_days": days,
        }
    recent_count, earliest_start, latest_start, cost_usd, partition_days = rows[0]
    recent_count = int(recent_count or 0)
    return {
        "status": "ok" if recent_count else "empty",
        "recent_count": int(recent_count or 0),
        "latest_start": str(latest_start) if latest_start is not None else None,
        "earliest_start": str(earliest_start) if earliest_start is not None else None,
        "cost_usd": float(cost_usd) if cost_usd is not None else 0.0,
        "partition_days": int(partition_days or 0),
        "window_days": days,
    }


def _span_metadata_completeness_for_window(
    con: duckdb.DuckDBPyConnection,
    *,
    days: int,
    warnings: list[str],
) -> dict:
    """Return bounded span metadata completeness by service and harness.

    This intentionally reads the partition-pruned ``spans`` view, not the
    cross-namespace ``spans_enriched`` join. It reports durable/native metadata
    completeness and classifies Mux routing spans as provenance-only when they
    lack a safe session/project/repo attribution path.
    """
    rows = _safe_rows(
        con,
        """
        WITH recent_spans AS (
          SELECT *
          FROM spans
          WHERE date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
            AND date <> '_seed'
        ),
        recent_openclaw_event_sessions AS (
          SELECT
            session_id,
            mode(
              CASE
                WHEN json_valid(raw_data)
                THEN NULLIF(json_extract_string(raw_data, '$.session_key'), '')
                ELSE NULL
              END
            ) AS session_key
          FROM agent_events
          WHERE date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
            AND session_id IS NOT NULL
            AND CASE
                  WHEN json_valid(raw_data)
                  THEN COALESCE(json_extract_string(raw_data, '$.harness'), '') = 'openclaw'
                  ELSE false
                END
          GROUP BY session_id
        ),
        unique_openclaw_event_session_keys AS (
          SELECT session_key
          FROM recent_openclaw_event_sessions
          WHERE session_key IS NOT NULL
          GROUP BY session_key
          HAVING count(DISTINCT session_id) = 1
        ),
        linked_openclaw_span_ids AS (
          SELECT DISTINCT s.trace_id, s.span_id
          FROM recent_spans s
          JOIN recent_openclaw_event_sessions e
            ON s.session_id = e.session_id
          WHERE COALESCE(s.harness, '') = 'openclaw'
            AND s.session_id IS NOT NULL

          UNION

          SELECT DISTINCT s.trace_id, s.span_id
          FROM recent_spans s
          JOIN recent_openclaw_event_sessions e
            ON s.session_key = e.session_key
          JOIN unique_openclaw_event_session_keys k
            ON k.session_key = e.session_key
          WHERE COALESCE(s.harness, '') = 'openclaw'
            AND s.session_key IS NOT NULL
        )
        SELECT
          COALESCE(s.service_name, '<unknown>') AS service_name,
          COALESCE(s.harness, '<unknown>') AS harness,
          count(*) AS total,
          count(*) FILTER (WHERE s.session_id IS NULL OR s.session_id = '') AS missing_session_id,
          count(*) FILTER (WHERE s.agent_id IS NULL OR s.agent_id = '') AS missing_agent_id,
          count(*) FILTER (WHERE s.project IS NULL OR s.project = '') AS missing_project,
          count(*) FILTER (WHERE s.repo_owner IS NULL OR s.repo_name IS NULL) AS missing_repo,
          count(*) FILTER (WHERE s.cwd IS NULL OR s.cwd = '') AS missing_cwd,
          count(*) FILTER (WHERE s.repository IS NULL OR s.repository = '') AS missing_repository,
          count(*) FILTER (WHERE l.span_id IS NOT NULL) AS linked_openclaw_spans,
          max(s.start_time) AS latest_start
        FROM recent_spans s
        LEFT JOIN linked_openclaw_span_ids l
          ON s.trace_id IS NOT DISTINCT FROM l.trace_id
         AND s.span_id = l.span_id
        GROUP BY 1, 2
        ORDER BY service_name, harness
        """,
        [days, days],
        warnings=warnings,
        label="span metadata completeness",
    )
    services: list[dict] = []
    totals = {
        "total": 0,
        "missing_session_id": 0,
        "missing_agent_id": 0,
        "missing_project": 0,
        "missing_repo": 0,
        "missing_cwd": 0,
        "missing_repository": 0,
        "linked_openclaw_spans": 0,
    }
    for (
        service_name,
        harness,
        total,
        missing_session_id,
        missing_agent_id,
        missing_project,
        missing_repo,
        missing_cwd,
        missing_repository,
        linked_openclaw_spans,
        latest_start,
    ) in rows:
        total_i = int(total or 0)
        row = {
            "service_name": str(service_name),
            "harness": str(harness),
            "total": total_i,
            "missing_session_id": int(missing_session_id or 0),
            "missing_agent_id": int(missing_agent_id or 0),
            "missing_project": int(missing_project or 0),
            "missing_repo": int(missing_repo or 0),
            "missing_cwd": int(missing_cwd or 0),
            "missing_repository": int(missing_repository or 0),
            "linked_openclaw_spans": int(linked_openclaw_spans or 0),
            "latest_start": str(latest_start) if latest_start is not None else None,
        }
        if row["missing_repo"] == 0 and row["missing_project"] == 0:
            row["classification"] = "attributed"
            row["attribution_path"] = "native_or_safe_derived_span_metadata"
        elif row["linked_openclaw_spans"] == total_i:
            row["classification"] = "linked_openclaw_spans"
            row["attribution_path"] = (
                "span metadata is linked non-destructively to native OpenClaw "
                "session events via stable session/session_key identifiers"
            )
        elif row["service_name"] == "mux-router" and (
            row["missing_session_id"] or row["missing_project"] or row["missing_repo"]
        ):
            row["classification"] = "provenance_only"
            row["attribution_path"] = (
                "Mux routing spans currently carry provider/model routing "
                "provenance only; do not infer repo/project from process "
                "command paths without explicit session or project attrs."
            )
        else:
            row["classification"] = "needs_attribution"
            row["attribution_path"] = (
                "emit explicit repo/project attrs or derive from safe cwd/repository/session links"
            )
        for key in totals:
            totals[key] += int(row[key])
        services.append(row)
    return {"window_days": days, "totals": totals, "services": services}


def _openclaw_agentweave_health_for_window(
    con: duckdb.DuckDBPyConnection,
    *,
    hours: int,
    days: int,
    warnings: list[str],
) -> dict:
    """Return bounded OpenClaw native / AgentWeave span contract health."""
    native_row = _safe_rows(
        con,
        """
        WITH recent AS (
            SELECT session_id, event_type, raw_data
            FROM agent_events
            WHERE lower(agent_id) LIKE '%openclaw%'
              AND date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
              AND TRY_CAST(timestamp AS TIMESTAMP) >= now() - ? * INTERVAL '1 hour'
        )
        SELECT
          count(*) AS total,
          count(*) FILTER (
            WHERE json_valid(raw_data)
              AND json_extract_string(raw_data, '$.harness') = 'openclaw'
          ) AS raw_harness_openclaw,
          count(*) FILTER (
            WHERE json_valid(raw_data)
              AND NULLIF(json_extract_string(raw_data, '$.session_key'), '') IS NOT NULL
          ) AS raw_session_key_present,
          count(*) FILTER (
            WHERE json_valid(raw_data)
              AND NULLIF(json_extract_string(raw_data, '$.session_uuid'), '') IS NOT NULL
          ) AS raw_session_uuid_present,
          count(*) FILTER (WHERE session_id = 'unknown_openclaw')
            AS unknown_openclaw_session_rows,
          count(*) FILTER (
            WHERE event_type IN (
              'user_turn', 'assistant_turn', 'tool_call', 'tool_result',
              'command', 'error', 'session_start', 'session_end', 'lifecycle'
            )
          ) AS normalized_event_type_rows
        FROM recent
        """,
        [days, hours],
        warnings=warnings,
        label="openclaw native contract health",
    )
    native_values = native_row[0] if native_row else (0, 0, 0, 0, 0, 0)
    native = {
        "total": int(native_values[0] or 0),
        "raw_harness_openclaw": int(native_values[1] or 0),
        "raw_session_key_present": int(native_values[2] or 0),
        "raw_session_uuid_present": int(native_values[3] or 0),
        "unknown_openclaw_session_rows": int(native_values[4] or 0),
        "normalized_event_type_rows": int(native_values[5] or 0),
    }

    historical_days = max(14, days)
    historical_row = _safe_rows(
        con,
        """
        SELECT count(*)
        FROM agent_events
        WHERE lower(agent_id) LIKE '%openclaw%'
          AND date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
          AND session_id = 'unknown_openclaw'
          AND (
            TRY_CAST(timestamp AS TIMESTAMP) < now() - ? * INTERVAL '1 hour'
            OR TRY_CAST(timestamp AS TIMESTAMP) IS NULL
          )
        """,
        [historical_days, hours],
        warnings=warnings,
        label="historical unknown_openclaw debt",
    )
    native["historical_unknown_openclaw_session_rows"] = int(
        (historical_row[0][0] if historical_row else 0) or 0
    )
    native["historical_unknown_openclaw_window_days"] = historical_days

    span_row = _safe_rows(
        con,
        """
        WITH recent AS (
            SELECT *
            FROM spans
            WHERE date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
              AND date <> '_seed'
              AND start_time >= now() - ? * INTERVAL '1 hour'
        ), flags AS (
            SELECT
              harness,
              session_id,
              session_key,
              project,
              repository,
              cwd,
              response_preview,
              preview_truncated,
              preview_bytes,
              CASE
                WHEN json_valid(attributes_json)
                THEN json_extract_string(attributes_json, '$."prov.harness"')
                ELSE NULL
              END AS attr_harness,
              CASE
                WHEN json_valid(attributes_json)
                THEN NULLIF(json_extract_string(attributes_json, '$."prov.session.key"'), '')
                ELSE NULL
              END AS attr_session_key
            FROM recent
        ), openish AS (
            SELECT * FROM flags WHERE harness = 'openclaw' OR attr_harness = 'openclaw'
        )
        SELECT
          count(*) FILTER (WHERE harness = 'openclaw') AS column_harness_openclaw,
          count(*) FILTER (WHERE attr_harness = 'openclaw') AS attr_harness_openclaw,
          count(*) FILTER (WHERE attr_harness = 'openclaw' AND harness IS NULL)
            AS attr_openclaw_but_column_harness_null,
          count(*) FILTER (WHERE attr_session_key IS NOT NULL AND session_key IS NULL)
            AS attr_session_key_but_column_null,
          count(*) AS openclaw_like_spans,
          count(*) FILTER (WHERE session_id IS NULL OR session_id = '') AS missing_session_id,
          count(*) FILTER (WHERE session_key IS NULL OR session_key = '') AS missing_session_key,
          count(*) FILTER (WHERE project IS NULL OR project = '') AS missing_project,
          count(*) FILTER (WHERE repository IS NULL OR repository = '') AS missing_repository,
          count(*) FILTER (WHERE cwd IS NULL OR cwd = '') AS missing_cwd,
          count(*) FILTER (WHERE response_preview IS NULL OR response_preview = '')
            AS missing_response_preview,
          count(*) FILTER (WHERE preview_truncated) AS preview_truncated_rows,
          max(preview_bytes) AS max_preview_bytes
        FROM openish
        """,
        [days, hours],
        warnings=warnings,
        label="agentweave openclaw span contract health",
    )
    span_values = span_row[0] if span_row else (0,) * 13
    spans = {
        "column_harness_openclaw": int(span_values[0] or 0),
        "attr_harness_openclaw": int(span_values[1] or 0),
        "attr_openclaw_but_column_harness_null": int(span_values[2] or 0),
        "attr_session_key_but_column_null": int(span_values[3] or 0),
        "openclaw_like_spans": int(span_values[4] or 0),
        "missing_session_id": int(span_values[5] or 0),
        "missing_session_key": int(span_values[6] or 0),
        "missing_project": int(span_values[7] or 0),
        "missing_repository": int(span_values[8] or 0),
        "missing_cwd": int(span_values[9] or 0),
        "missing_response_preview": int(span_values[10] or 0),
        "preview_truncated_rows": int(span_values[11] or 0),
        "max_preview_bytes": int(span_values[12] or 0),
    }

    link_row = _safe_rows(
        con,
        """
        WITH native_session_ids AS (
            SELECT DISTINCT session_id
            FROM agent_events
            WHERE lower(agent_id) LIKE '%openclaw%'
              AND date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
              AND TRY_CAST(timestamp AS TIMESTAMP) >= now() - ? * INTERVAL '1 hour'
              AND session_id IS NOT NULL
              AND session_id <> ''
              AND session_id <> 'unknown_openclaw'
        ), native_session_keys AS (
            SELECT DISTINCT NULLIF(json_extract_string(raw_data, '$.session_key'), '') AS session_key
            FROM agent_events
            WHERE lower(agent_id) LIKE '%openclaw%'
              AND date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
              AND TRY_CAST(timestamp AS TIMESTAMP) >= now() - ? * INTERVAL '1 hour'
              AND json_valid(raw_data)
              AND NULLIF(json_extract_string(raw_data, '$.session_key'), '') IS NOT NULL
        ), recent_spans AS (
            SELECT
              session_id,
              session_key,
              harness,
              CASE
                WHEN json_valid(attributes_json)
                THEN json_extract_string(attributes_json, '$."prov.harness"')
                ELSE NULL
              END AS attr_harness,
              CASE
                WHEN json_valid(attributes_json)
                THEN NULLIF(json_extract_string(attributes_json, '$."prov.session.key"'), '')
                ELSE NULL
              END AS attr_session_key
            FROM spans
            WHERE date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
              AND date <> '_seed'
              AND start_time >= now() - ? * INTERVAL '1 hour'
        ), openish AS (
            SELECT
              *,
              EXISTS (
                SELECT 1 FROM native_session_ids n WHERE n.session_id = recent_spans.session_id
              ) AS exact_match,
              (
                recent_spans.session_key IS NOT NULL
                AND EXISTS (
                  SELECT 1
                  FROM native_session_keys n
                  WHERE n.session_key = recent_spans.session_key
                )
              )
              OR (
                recent_spans.attr_session_key IS NOT NULL
                AND EXISTS (
                  SELECT 1
                  FROM native_session_keys n
                  WHERE n.session_key = recent_spans.attr_session_key
                )
              ) AS session_key_match
            FROM recent_spans
            WHERE harness = 'openclaw' OR attr_harness = 'openclaw'
        )
        SELECT
          count(*) AS openclaw_like_spans,
          count(*) FILTER (WHERE exact_match) AS exact_session_id_matches,
          count(*) FILTER (WHERE session_key_match) AS session_key_matches,
          count(*) FILTER (WHERE exact_match OR session_key_match) AS matched_spans,
          count(*) FILTER (WHERE NOT (exact_match OR session_key_match)) AS unmatched_spans
        FROM openish
        """,
        [days, hours, days, hours, days, hours],
        warnings=warnings,
        label="openclaw agentweave linkability",
    )
    link_values = link_row[0] if link_row else (0, 0, 0, 0, 0)
    linkability = {
        "openclaw_like_spans": int(link_values[0] or 0),
        "exact_session_id_matches": int(link_values[1] or 0),
        "session_key_matches": int(link_values[2] or 0),
        "matched_spans": int(link_values[3] or 0),
        "unmatched_spans": int(link_values[4] or 0),
    }

    status = "ok"
    if native["unknown_openclaw_session_rows"]:
        status = "warn"
    elif native["total"] == 0 and spans["openclaw_like_spans"] == 0:
        if not native["historical_unknown_openclaw_session_rows"]:
            status = "missing"
    elif linkability["unmatched_spans"]:
        status = "warn"
    elif native["total"] and native["raw_harness_openclaw"] == 0:
        status = "warn"
    elif spans["attr_openclaw_but_column_harness_null"]:
        status = "warn"

    return {
        "status": status,
        "native_events": native,
        "spans": spans,
        "linkability": linkability,
    }


def _embedding_status(
    *,
    embed_counts: dict[str, int],
    session_embeddings_count: int | None,
) -> dict[str, str]:
    """Summarize whether the embeddings queue appears to be making progress."""
    pending = int(embed_counts.get("pending", 0))
    running = int(embed_counts.get("running", 0))
    errored = int(embed_counts.get("errored", 0))
    done = int(embed_counts.get("done", 0))

    if not embed_counts:
        return {
            "state": "unknown",
            "message": "embed_jobs table is missing or empty; no embedding queue activity found",
        }
    if pending == 0 and running == 0:
        if errored:
            return {
                "state": "errors",
                "message": f"{errored} embed jobs are errored; inspect recent_errors for details",
            }
        return {
            "state": "idle",
            "message": f"no pending embed jobs ({done} done, {session_embeddings_count or 0} session embeddings)",
        }
    if running:
        return {
            "state": "active",
            "message": f"{running} running and {pending} pending embed jobs",
        }
    if pending and (session_embeddings_count or 0) == 0:
        return {
            "state": "offline_or_unconfigured",
            "message": (
                f"{pending} pending embed jobs but 0 session_embeddings; embeddings likely "
                "offline or missing local Ollama GPU config. start drover-server run without "
                "--no-embeddings and configure [summarizer] local_ollama_url or "
                "gpu_relay_url/gpu_ollama_url."
            ),
        }
    return {
        "state": "backlog",
        "message": f"{pending} pending embed jobs waiting for the embeddings worker",
    }


def _unprocessed_incoming_paths(incoming_dir: Path) -> list[Path]:
    incoming_dir = Path(incoming_dir)
    if not incoming_dir.exists():
        return []
    files: list[Path] = []
    for path in sorted(incoming_dir.rglob("*.jsonl")):
        try:
            rel = path.relative_to(incoming_dir)
        except ValueError:
            continue
        if ".processed" in rel.parts:
            continue
        files.append(path)
    return files


def _unprocessed_incoming(incoming_dir: Path) -> list[str]:
    incoming_dir = Path(incoming_dir)
    return [
        path.relative_to(incoming_dir).as_posix()
        for path in _unprocessed_incoming_paths(incoming_dir)
    ]


def _format_age(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def _pending_incoming_by_source(
    incoming_dir: Path, *, now: datetime | None = None
) -> dict[str, dict]:
    incoming_dir = Path(incoming_dir)
    if now is None:
        now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    grouped: dict[str, dict] = {}
    for path in _unprocessed_incoming_paths(incoming_dir):
        rel = path.relative_to(incoming_dir)
        source = rel.parts[0] if len(rel.parts) > 1 else "<root>"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        age = max(0, int(now_ts - mtime))
        row = grouped.setdefault(
            source,
            {
                "count": 0,
                "oldest_age_seconds": 0,
                "oldest_age_human": "0s",
                "oldest_file": rel.as_posix(),
            },
        )
        row["count"] += 1
        if age >= int(row["oldest_age_seconds"]):
            row["oldest_age_seconds"] = age
            row["oldest_age_human"] = _format_age(age)
            row["oldest_file"] = rel.as_posix()
    return dict(sorted(grouped.items()))


def _stale_running_span_embed_jobs(
    con: duckdb.DuckDBPyConnection,
    *,
    stale_after_hours: int,
    limit: int = 10,
    warnings: list[str],
) -> dict:
    count_rows = _safe_rows(
        con,
        """
        SELECT count(*),
               max(date_diff('hour', COALESCE(updated_at, enqueued_at), now()))
          FROM span_embed_jobs
         WHERE status = 'running'
           AND COALESCE(updated_at, enqueued_at) < now() - (? * INTERVAL '1 hour')
        """,
        [stale_after_hours],
        warnings=warnings,
        label="stale span embed job count",
    )
    total_stale = int(count_rows[0][0] or 0) if count_rows else 0
    max_age = int(count_rows[0][1] or 0) if count_rows else 0
    rows = _safe_rows(
        con,
        """
        SELECT span_id,
               attempts,
               COALESCE(updated_at, enqueued_at) AS last_touched_at,
               date_diff('hour', COALESCE(updated_at, enqueued_at), now()) AS age_hours
          FROM span_embed_jobs
         WHERE status = 'running'
           AND COALESCE(updated_at, enqueued_at) < now() - (? * INTERVAL '1 hour')
         ORDER BY COALESCE(updated_at, enqueued_at) ASC NULLS FIRST, span_id
         LIMIT ?
        """,
        [stale_after_hours, limit],
        warnings=warnings,
        label="stale span embed jobs",
    )
    stale = [
        {
            "span_id": str(span_id),
            "attempts": int(attempts or 0),
            "last_touched_at": (
                str(last_touched_at) if last_touched_at is not None else None
            ),
            "age_hours": int(age_hours or 0),
        }
        for span_id, attempts, last_touched_at, age_hours in rows
    ]
    return {
        "stale_running": stale,
        "stale_running_jobs": total_stale,
        "stale_running_age_hours": max_age,
    }


def _embedded_recent_span_count(
    con: duckdb.DuckDBPyConnection,
    *,
    days: int,
    warnings: list[str],
) -> int | None:
    rows = _safe_rows(
        con,
        """
        SELECT count(DISTINCT e.span_id)
          FROM span_embeddings e
          JOIN spans s ON s.span_id = e.span_id
         WHERE s.date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
           AND s.date <> '_seed'
        """,
        [days],
        warnings=warnings,
        label="recent span embedding coverage",
    )
    if not rows:
        return None
    return int(rows[0][0] or 0)


def _bundle_quality_summary(
    con: duckdb.DuckDBPyConnection,
    *,
    warnings: list[str],
) -> dict[str, int | float | None]:
    rows = _safe_rows(
        con,
        """
        SELECT
          count(*) AS total_summaries,
          count(*) FILTER (
            WHERE NULLIF(trim(COALESCE(ss.summary_md, '')), '') IS NOT NULL
          ) AS summaries_with_summary_md,
          count(*) FILTER (
            WHERE NULLIF(trim(COALESCE(ss.next_steps_md, '')), '') IS NOT NULL
          ) AS summaries_with_next_steps_md,
          count(*) FILTER (
            WHERE ss.files_touched IS NOT NULL AND array_length(ss.files_touched) > 0
          ) AS summaries_with_files_touched,
          count(*) FILTER (
            WHERE ss.open_questions IS NOT NULL AND array_length(ss.open_questions) > 0
          ) AS summaries_with_open_questions,
          count(*) FILTER (
            WHERE NULLIF(trim(COALESCE(ss.last_user_prompt, '')), '') IS NOT NULL
          ) AS summaries_with_last_user_prompt,
          count(*) FILTER (
            WHERE NULLIF(trim(COALESCE(ss.last_assistant, '')), '') IS NOT NULL
          ) AS summaries_with_last_assistant,
          count(*) FILTER (
            WHERE NULLIF(trim(COALESCE(ss.generator_model, '')), '') IS NOT NULL
          ) AS summaries_with_generator_model,
          count(*) FILTER (
            WHERE COALESCE(ss.status, '') IN ('complete', 'completed')
          ) AS complete_summaries,
          count(*) FILTER (
            WHERE NULLIF(trim(COALESCE(ss.summary_md, '')), '') IS NOT NULL
              AND COALESCE(ss.status, '') IN ('complete', 'completed')
              AND se.session_id IS NOT NULL
          ) AS recall_usable_summaries,
          count(*) FILTER (
            WHERE NULLIF(trim(COALESCE(ss.summary_md, '')), '') IS NOT NULL
              AND NULLIF(trim(COALESCE(ss.next_steps_md, '')), '') IS NOT NULL
              AND NULLIF(trim(COALESCE(ss.last_user_prompt, '')), '') IS NOT NULL
              AND NULLIF(trim(COALESCE(ss.last_assistant, '')), '') IS NOT NULL
              AND (
                (ss.files_touched IS NOT NULL AND array_length(ss.files_touched) > 0)
                OR (ss.open_questions IS NOT NULL AND array_length(ss.open_questions) > 0)
              )
          ) AS rich_bundle_ready_summaries
        FROM session_summaries ss
        LEFT JOIN session_embeddings se USING (session_id)
        """,
        warnings=warnings,
        label="bundle quality",
    )
    if not rows:
        return {
            "total_summaries": None,
            "summaries_with_summary_md": None,
            "summaries_with_next_steps_md": None,
            "summaries_with_files_touched": None,
            "summaries_with_open_questions": None,
            "summaries_with_last_user_prompt": None,
            "summaries_with_last_assistant": None,
            "summaries_with_generator_model": None,
            "complete_summaries": None,
            "recall_usable_summaries": None,
            "recall_usable_percent": None,
            "missing_recall_processing_summaries": None,
            "missing_rich_evidence_summaries": None,
            "bundle_ready_summaries": None,
            "bundle_ready_percent": None,
        }
    (
        total_summaries,
        summaries_with_summary_md,
        summaries_with_next_steps_md,
        summaries_with_files_touched,
        summaries_with_open_questions,
        summaries_with_last_user_prompt,
        summaries_with_last_assistant,
        summaries_with_generator_model,
        complete_summaries,
        recall_usable_summaries,
        rich_bundle_ready_summaries,
    ) = rows[0]
    total = int(total_summaries or 0)
    usable = int(recall_usable_summaries or 0)
    ready = int(rich_bundle_ready_summaries or 0)
    usable_percent = None
    ready_percent = None
    if total:
        usable_percent = round((usable / total) * 100.0, 1)
        ready_percent = round((ready / total) * 100.0, 1)
    return {
        "total_summaries": total,
        "summaries_with_summary_md": int(summaries_with_summary_md or 0),
        "summaries_with_next_steps_md": int(summaries_with_next_steps_md or 0),
        "summaries_with_files_touched": int(summaries_with_files_touched or 0),
        "summaries_with_open_questions": int(summaries_with_open_questions or 0),
        "summaries_with_last_user_prompt": int(summaries_with_last_user_prompt or 0),
        "summaries_with_last_assistant": int(summaries_with_last_assistant or 0),
        "summaries_with_generator_model": int(summaries_with_generator_model or 0),
        "complete_summaries": int(complete_summaries or 0),
        "recall_usable_summaries": usable,
        "recall_usable_percent": usable_percent,
        "missing_recall_processing_summaries": max(total - usable, 0),
        "missing_rich_evidence_summaries": max(total - ready, 0),
        "bundle_ready_summaries": ready,
        "bundle_ready_percent": ready_percent,
    }


def runtime_audit(
    *,
    duckdb_path: Path,
    incoming_dir: Optional[Path] = None,
    hours: int = 24,
    now: datetime | None = None,
    source_duckdb_path: Optional[Path] = None,
    diagnostic_db_path: Optional[Path] = None,
    deep: bool = True,
    role: str = "diagnostic",
) -> dict:
    """Return a read-only operational health report for a Drover runtime DB.

    Missing tables/views are represented with ``None`` counts and empty
    sections. The function does not bootstrap schemas or mutate the database.

    ``role`` picks the DuckDB connection profile. It defaults to
    ``diagnostic`` (one thread) because this function is reachable from the
    CLI and from in-process diagnostics pointed at the *live* database, where
    ``threads`` is instance-wide and extra threads starve every other live
    reader (#91). Callers that handed us a private copy -- ``/metrics`` does
    -- should pass ``role="snapshot"`` for parallelism they cannot leak.
    """
    duckdb_path = Path(duckdb_path)
    source_duckdb_path = Path(source_duckdb_path) if source_duckdb_path else duckdb_path
    diagnostic_db_path = Path(diagnostic_db_path) if diagnostic_db_path else None
    hours = max(1, int(hours))
    report: dict = {
        "duckdb_path": str(duckdb_path),
        "source_duckdb_path": str(source_duckdb_path),
        "diagnostic_duckdb_path": (
            str(diagnostic_db_path) if diagnostic_db_path else None
        ),
        "incoming_dir": str(incoming_dir) if incoming_dir else None,
        "hours": hours,
        "diagnostic_depth": "deep" if deep else "standard",
        "skipped_checks": [],
        "table_counts": {},
        "latest_events": {},
        "summarize_jobs": {
            "status_counts": {},
            "recent_errors": [],
            "backend_health": {"state": "missing"},
        },
        "embed_jobs": {"status_counts": {}, "recent_errors": []},
        "span_embed_jobs": {
            "status_counts": {},
            "recent_errors": [],
            "running_jobs": 0,
            "stale_running_jobs": 0,
            "stale_running_age_hours": 0,
            "stale_running": [],
        },
        "session_embeddings_count": None,
        "span_embedding_coverage": {
            "embedded_spans": None,
            "embedded_recent_spans": None,
            "pending_jobs": None,
            "stale_running_jobs": None,
            "total_recent_spans": None,
            "coverage_percent": None,
            "coverage_note": None,
        },
        "bundle_quality": {
            "total_summaries": None,
            "summaries_with_summary_md": None,
            "summaries_with_next_steps_md": None,
            "summaries_with_files_touched": None,
            "summaries_with_open_questions": None,
            "summaries_with_last_user_prompt": None,
            "summaries_with_last_assistant": None,
            "summaries_with_generator_model": None,
            "complete_summaries": None,
            "recall_usable_summaries": None,
            "recall_usable_percent": None,
            "missing_recall_processing_summaries": None,
            "missing_rich_evidence_summaries": None,
            "bundle_ready_summaries": None,
            "bundle_ready_percent": None,
        },
        "embedding_status": {
            "state": "unknown",
            "message": "embedding status unavailable until embed_jobs is readable",
        },
        "session_consistency": {"status": "missing"},
        "agent_event_identity": {"status": "missing"},
        "span_health": {
            "status": "missing",
            "recent_count": None,
            "latest_start": None,
            "earliest_start": None,
            "cost_usd": None,
            "partition_days": None,
        },
        "span_metadata_completeness": {
            "window_days": None,
            "totals": {},
            "services": [],
        },
        "openclaw_agentweave_health": {
            "status": "missing",
            "native_events": {},
            "spans": {},
            "linkability": {},
        },
        "repo_attribution": {},
        "repo_attribution_windows": {"24h": {}, "7d": {}},
        "top_unattributed_cwds": {"24h": [], "7d": []},
        "general_workspace_cwds": {"24h": [], "7d": []},
        "claude_attribution_gap_categories": {"24h": {}, "7d": {}},
        "unprocessed_incoming": (
            _unprocessed_incoming(Path(incoming_dir)) if incoming_dir else []
        ),
        "pending_incoming_by_source": (
            _pending_incoming_by_source(Path(incoming_dir), now=now)
            if incoming_dir
            else {}
        ),
        "warnings": [],
    }
    if not duckdb_path.exists():
        report["warnings"].append(f"DuckDB path does not exist: {duckdb_path}")
        for name in RUNTIME_KEY_RELATIONS:
            report["table_counts"][name] = None
        return report

    try:
        con = open_duckdb_connection(duckdb_path, read_only=True, role=role)
    except duckdb.Error as e:
        report["warnings"].append(f"failed to open DuckDB read-only: {e}")
        for name in RUNTIME_KEY_RELATIONS:
            report["table_counts"][name] = None
        return report

    try:
        recent_days = max(2, int(ceil(hours / 24)) + 2)
        # One pass over the whole agent_events history, shared by the row
        # count, the session-set metrics and the duplicate-identity metrics.
        # Each of those used to scan the parquet tree on its own -- 1.61s,
        # 2.37s and 2.71s of an 11.4s audit at 6,876 files (#78) -- for three
        # columns between them. `None` means the pass was unavailable and
        # every consumer reads agent_events directly, as it did before.
        scan = scan_agent_events_once(con)
        for name in RUNTIME_KEY_RELATIONS:
            if name == "spans":
                continue
            if name == "agent_events" and scan is not None:
                report["table_counts"][name] = scan.total_rows
                continue
            report["table_counts"][name] = _relation_count(con, name)
        if _relation_exists(con, "spans"):
            report["span_health"] = _span_health_for_window(
                con, days=recent_days, warnings=report["warnings"]
            )
            report["span_metadata_completeness"] = (
                _span_metadata_completeness_for_window(
                    con, days=recent_days, warnings=report["warnings"]
                )
            )
            report["table_counts"]["spans"] = report["span_health"].get("recent_count")
        else:
            report["table_counts"]["spans"] = None

        if (
            report["table_counts"].get("agent_events") is not None
            and report["table_counts"].get("spans") is not None
        ):
            report["openclaw_agentweave_health"] = (
                _openclaw_agentweave_health_for_window(
                    con, hours=hours, days=recent_days, warnings=report["warnings"]
                )
            )

        rows = []
        if report["table_counts"].get("agent_events") is not None:
            rows = _safe_rows(
                con,
                """
            SELECT
                agent_id,
                arg_max(timestamp, event_ts) AS timestamp,
                arg_max(event_type, event_ts) AS event_type,
                arg_max(session_id, event_ts) AS session_id,
                arg_max(repo_owner, event_ts) AS repo_owner,
                arg_max(repo_name, event_ts) AS repo_name
            FROM (
                SELECT
                    agent_id,
                    timestamp,
                    event_type,
                    session_id,
                    repo_owner,
                    repo_name,
                    TRY_CAST(timestamp AS TIMESTAMP) AS event_ts
                FROM agent_events
                WHERE agent_id IS NOT NULL
                  AND date >= strftime(current_date - ? * INTERVAL '1 day', '%Y-%m-%d')
            )
            WHERE event_ts IS NOT NULL
            GROUP BY agent_id
            ORDER BY agent_id
            """,
                [recent_days],
                warnings=report["warnings"],
                label="latest events",
            )
        report["latest_events"] = {
            str(agent_id): {
                "timestamp": str(ts) if ts is not None else None,
                "event_type": event_type,
                "session_id": session_id,
                "repo": (
                    f"{repo_owner}/{repo_name}" if repo_owner and repo_name else None
                ),
            }
            for agent_id, ts, event_type, session_id, repo_owner, repo_name in rows
        }

        summarize_counts = _status_counts(con, "summarize_jobs")
        summarize_errors = _recent_job_errors(con, "summarize_jobs")
        report["summarize_jobs"] = {
            "status_counts": summarize_counts,
            "recent_errors": summarize_errors,
            "backend_health": _summarize_job_backend_health(con, summarize_counts),
        }
        embed_counts = _status_counts(con, "embed_jobs")
        report["embed_jobs"] = {
            "status_counts": embed_counts,
            "recent_errors": _recent_job_errors(con, "embed_jobs"),
        }
        report["session_embeddings_count"] = report["table_counts"].get(
            "session_embeddings"
        )
        span_embed_counts = _status_counts(con, "span_embed_jobs")
        stale_span_jobs = {
            "stale_running": [],
            "stale_running_jobs": 0,
            "stale_running_age_hours": 0,
        }
        if report["table_counts"].get("span_embed_jobs") is not None:
            stale_span_jobs = _stale_running_span_embed_jobs(
                con,
                stale_after_hours=24,
                warnings=report["warnings"],
            )
        report["span_embed_jobs"] = {
            "status_counts": span_embed_counts,
            "recent_errors": [],
            "running_jobs": int(span_embed_counts.get("running", 0)),
            **stale_span_jobs,
        }
        if stale_span_jobs["stale_running_jobs"]:
            report["warnings"].append(
                "span_embed_jobs has "
                f"{stale_span_jobs['stale_running_jobs']} stale running job(s); "
                "operator flow: run `drover-server embeddings reset-stale-spans` "
                "to preview and add `--apply` to requeue them"
            )
        embedded_spans = report["table_counts"].get("span_embeddings")
        total_recent_spans = report["span_health"].get("recent_count")
        embedded_recent_spans = None
        if total_recent_spans is not None and _relation_exists(con, "spans"):
            embedded_recent_spans = _embedded_recent_span_count(
                con, days=recent_days, warnings=report["warnings"]
            )
        coverage_percent = None
        if total_recent_spans:
            coverage_percent = round(
                (int(embedded_recent_spans or 0) / int(total_recent_spans)) * 100, 1
            )
            coverage_percent = min(100.0, coverage_percent)
        coverage_note = None
        if (
            embedded_spans is not None
            and total_recent_spans is not None
            and int(embedded_spans or 0) > int(total_recent_spans or 0)
        ):
            coverage_note = (
                "embedded_spans is the total span_embeddings row count; derived or "
                "historical embeddings can exceed the current recent span denominator. "
                "coverage_percent uses embedded_recent_spans / total_recent_spans and is bounded at 100%."
            )
        report["span_embedding_coverage"] = {
            "embedded_spans": embedded_spans,
            "embedded_recent_spans": embedded_recent_spans,
            "pending_jobs": int(span_embed_counts.get("pending", 0)),
            "stale_running_jobs": int(stale_span_jobs["stale_running_jobs"]),
            "total_recent_spans": total_recent_spans,
            "coverage_percent": coverage_percent,
            "coverage_note": coverage_note,
        }
        report["embedding_status"] = _embedding_status(
            embed_counts=embed_counts,
            session_embeddings_count=report["session_embeddings_count"],
        )
        if report["embedding_status"]["state"] == "offline_or_unconfigured":
            pending = int(embed_counts.get("pending", 0))
            report["warnings"].append(
                f"Embedding queue has {pending} pending jobs but 0 session_embeddings"
            )
        if report["table_counts"].get("agent_events") is not None:
            report["session_consistency"] = audit_session_consistency(
                con,
                duckdb_path=duckdb_path,
                include_expensive_checks=deep,
                scan=scan,
            )
            session_status = report["session_consistency"].get("status")
            if session_status not in {"ok", "missing"}:
                report["warnings"].append(
                    f"session consistency audit status: {session_status}"
                )
            for warning in report["session_consistency"].get("warnings", []):
                if warning not in report["warnings"]:
                    report["warnings"].append(warning)

        if report["table_counts"].get("agent_events") is not None:
            report["agent_event_identity"] = audit_agent_event_identity(con, scan=scan)
            identity = report["agent_event_identity"]
            if identity.get("duplicate_dedup_key_values", 0):
                report["warnings"].append(
                    "agent_events has duplicate dedup_key values; canonical event "
                    "dedupe is not clean"
                )

        if report["table_counts"].get("session_summaries") is not None:
            report["bundle_quality"] = _bundle_quality_summary(
                con, warnings=report["warnings"]
            )

        if deep and report["table_counts"].get("agent_events") is not None:
            report["repo_attribution"] = _repo_attribution_for_window(
                con, hours=hours, warnings=report["warnings"]
            )
            report["repo_attribution_windows"] = {
                "24h": _repo_attribution_for_window(
                    con, hours=24, warnings=report["warnings"]
                ),
                "7d": _repo_attribution_for_window(
                    con, hours=24 * 7, warnings=report["warnings"]
                ),
            }
            report["top_unattributed_cwds"] = {
                "24h": _top_unattributed_cwds_for_window(
                    con, hours=24, warnings=report["warnings"]
                ),
                "7d": _top_unattributed_cwds_for_window(
                    con, hours=24 * 7, warnings=report["warnings"]
                ),
            }
            report["general_workspace_cwds"] = {
                "24h": _general_workspace_cwds_for_window(
                    con, hours=24, warnings=report["warnings"]
                ),
                "7d": _general_workspace_cwds_for_window(
                    con, hours=24 * 7, warnings=report["warnings"]
                ),
            }
            report["claude_attribution_gap_categories"] = {
                "24h": _claude_attribution_gap_categories_for_window(
                    con, hours=24, warnings=report["warnings"]
                ),
                "7d": _claude_attribution_gap_categories_for_window(
                    con, hours=24 * 7, warnings=report["warnings"]
                ),
            }
        elif report["table_counts"].get("agent_events") is not None:
            report["skipped_checks"].append(
                "repo attribution and cwd gap diagnostics require deep mode"
            )
    finally:
        con.close()
    return report


def _format_status_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "missing/empty"
    return ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))


def format_runtime_audit(report: dict) -> str:
    """Format ``runtime_audit`` output as a concise human-readable report."""
    lines: list[str] = [
        "Drover runtime audit",
        "====================",
        f"source_db     : {report.get('source_duckdb_path') or report.get('duckdb_path')}",
        f"diagnostic_db : {report.get('diagnostic_duckdb_path') or 'none'}",
        f"incoming_dir  : {report.get('incoming_dir') or '-'}",
        "",
        "table counts:",
    ]
    for name, count in report.get("table_counts", {}).items():
        value = "missing" if count is None else str(count)
        lines.append(f"  {name:20s} {value}")

    span_health = report.get("span_health", {})
    lines.extend(
        [
            "",
            "span health:",
            f"  status={span_health.get('status', 'missing')} recent_count={span_health.get('recent_count') if span_health.get('recent_count') is not None else 'missing'} window_days={span_health.get('window_days', '-')}",
            f"  latest_start={span_health.get('latest_start') or '-'} cost_usd={span_health.get('cost_usd') if span_health.get('cost_usd') is not None else '-'}",
        ]
    )

    span_meta = report.get("span_metadata_completeness", {})
    services = span_meta.get("services", [])
    lines.extend(["", "span metadata completeness by service:"])
    if services:
        for row in services:
            lines.append(
                f"  {row.get('service_name', '<unknown>'):25s} "
                f"harness={row.get('harness', '<unknown>')} "
                f"class={row.get('classification', 'unknown')} "
                f"rows={row.get('total', 0)} "
                f"missing_session={row.get('missing_session_id', 0)} "
                f"missing_project={row.get('missing_project', 0)} "
                f"missing_repo={row.get('missing_repo', 0)} "
                f"linked_openclaw={row.get('linked_openclaw_spans', 0)}"
            )
    else:
        lines.append("  none")

    ocaw = report.get("openclaw_agentweave_health", {})
    if ocaw and ocaw.get("status") != "missing":
        native = ocaw.get("native_events", {})
        spans = ocaw.get("spans", {})
        linkability = ocaw.get("linkability", {})
        lines.extend(
            [
                "",
                "OpenClaw/AgentWeave contract health:",
                f"  status={ocaw.get('status', 'unknown')}",
                "  native events: "
                f"total={native.get('total', 0)} "
                f"raw_harness_openclaw={native.get('raw_harness_openclaw', 0)} "
                f"raw_session_key={native.get('raw_session_key_present', 0)} "
                f"raw_session_uuid={native.get('raw_session_uuid_present', 0)} "
                f"active_unknown_openclaw={native.get('unknown_openclaw_session_rows', 0)}",
                "  legacy/historical native events: "
                f"historical_unknown_openclaw={native.get('historical_unknown_openclaw_session_rows', 0)} "
                f"window_days={native.get('historical_unknown_openclaw_window_days', '-')}; "
                "excluded from live contract severity",
                "  spans: "
                f"column_harness_openclaw={spans.get('column_harness_openclaw', 0)} "
                f"attr_harness_openclaw={spans.get('attr_harness_openclaw', 0)} "
                f"attr_openclaw_column_null={spans.get('attr_openclaw_but_column_harness_null', 0)} "
                f"attr_session_key_column_null={spans.get('attr_session_key_but_column_null', 0)}",
                "  linkability: "
                f"openclaw_like_spans={linkability.get('openclaw_like_spans', 0)} "
                f"exact_session_id={linkability.get('exact_session_id_matches', 0)} "
                f"session_key={linkability.get('session_key_matches', 0)} "
                f"unmatched={linkability.get('unmatched_spans', 0)}",
            ]
        )

    lines.extend(["", "latest event by agent:"])
    latest = report.get("latest_events", {})
    if latest:
        for agent, row in latest.items():
            repo = row.get("repo") or "-"
            lines.append(
                f"  {agent:25s} {row.get('timestamp') or '-'}  {row.get('event_type') or '-'}  {repo}"
            )
    else:
        lines.append("  none")

    sj = report.get("summarize_jobs", {})
    sj_health = sj.get("backend_health", {})
    lines.extend(
        [
            "",
            f"summarize_jobs: {_format_status_counts(sj.get('status_counts', {}))}",
            "summarizer health: "
            f"{sj_health.get('state', 'missing')} "
            f"pending={sj_health.get('pending', 0)} running={sj_health.get('running', 0)} "
            f"retryable={sj_health.get('retryable_errors', 0)} "
            f"non_retryable={sj_health.get('non_retryable_errors', 0)}",
        ]
    )
    errors = sj.get("recent_errors", [])
    if errors:
        lines.append("  recent non-done errors:")
        for err in errors:
            retryable = "yes" if err.get("retryable") else "no"
            lines.append(
                f"  - {err.get('session_id')}: {err.get('status')} attempts={err.get('attempts')} "
                f"category={err.get('error_category', 'unknown')} retryable={retryable} "
                f"{err.get('last_error_summary') or '-'}"
            )

    ej = report.get("embed_jobs", {})
    embedding_status = report.get("embedding_status", {})
    lines.extend(
        [
            "",
            f"embed_jobs: {_format_status_counts(ej.get('status_counts', {}))}",
            f"session_embeddings: {report.get('session_embeddings_count') if report.get('session_embeddings_count') is not None else 'missing'}",
            f"embedding status: {embedding_status.get('state', 'unknown')} — {embedding_status.get('message', '-')}",
        ]
    )
    sej = report.get("span_embed_jobs", {})
    span_cov = report.get("span_embedding_coverage", {})
    stale_running = int(sej.get("stale_running_jobs") or 0)
    stale_suffix = ""
    if stale_running:
        stale_suffix = (
            f" (stale_running={stale_running} "
            f"max_age_hours={sej.get('stale_running_age_hours', 0)})"
        )
    coverage_detail = ""
    if span_cov.get("embedded_recent_spans") is not None:
        coverage_detail = (
            f" ({span_cov.get('embedded_spans')} total embedded; "
            f"{span_cov.get('embedded_recent_spans')} in recent span denominator)"
        )
    lines.extend(
        [
            f"span_embed_jobs: {_format_status_counts(sej.get('status_counts', {}))}{stale_suffix}",
            "span embeddings: "
            f"{span_cov.get('embedded_spans') if span_cov.get('embedded_spans') is not None else 'missing'} "
            f"embedded; pending={span_cov.get('pending_jobs') if span_cov.get('pending_jobs') is not None else 'missing'} "
            f"stale_running={span_cov.get('stale_running_jobs') if span_cov.get('stale_running_jobs') is not None else 'missing'} "
            f"coverage={span_cov.get('coverage_percent') if span_cov.get('coverage_percent') is not None else '-'}%"
            f"{coverage_detail}",
        ]
    )
    if span_cov.get("coverage_note"):
        lines.append(f"  note: {span_cov.get('coverage_note')}")

    session_consistency = report.get("session_consistency", {})
    if session_consistency.get("status") == "missing":
        lines.extend(["", "session consistency: missing"])
    else:
        lines.extend(
            [
                "",
                "session consistency: "
                f"status={session_consistency.get('status', 'unknown')} "
                f"sessions_relation={session_consistency.get('sessions_relation_type') or 'missing'} "
                f"event_sessions={session_consistency.get('event_sessions')} "
                f"sessions_rows={session_consistency.get('sessions_rows')} "
                f"missing_session_rows={session_consistency.get('event_sessions_missing_session_row')} "
                f"orphan_session_rows={session_consistency.get('session_rows_without_events')} "
                f"event_count_mismatches={session_consistency.get('event_count_mismatches')} "
                f"missing_summaries={session_consistency.get('event_sessions_without_summary')} "
                f"orphan_summaries={session_consistency.get('summaries_without_events')}",
            ]
        )

    identity = report.get("agent_event_identity", {})
    if identity.get("status") == "missing":
        lines.extend(["", "agent_event identity: missing"])
    else:
        lines.extend(
            [
                "",
                "agent_event identity: "
                f"canonical={identity.get('canonical_semantics', 'dedup_key')} "
                f"source_id_context={identity.get('source_id_context', 'source/provenance only')} "
                f"duplicate_id_values={identity.get('duplicate_id_values', 0)} "
                f"duplicate_id_rows={identity.get('duplicate_id_rows', 0)} "
                f"duplicate_dedup_key_values={identity.get('duplicate_dedup_key_values', 0)} "
                f"duplicate_dedup_key_rows={identity.get('duplicate_dedup_key_rows', 0)}",
            ]
        )
        examples = identity.get("duplicate_id_examples", [])
        if examples:
            lines.append("  duplicate id examples:")
            for row in examples:
                lines.append(
                    f"  - {row.get('id')}: rows={row.get('rows')} "
                    f"dedup_keys={row.get('dedup_keys')}"
                )

    lines.extend(
        [
            "",
            f"repo attribution (last {report.get('hours')}h):",
        ]
    )
    attribution = report.get("repo_attribution", {})
    if attribution:
        for agent, row in attribution.items():
            lines.append(
                f"  {agent:25s} {row.get('percent'):5.1f}% "
                f"({row.get('attributed')}/{row.get('project_total', row.get('total'))} project, "
                f"{row.get('general_workspace', 0)} general)"
            )
    else:
        lines.append("  none")

    windows = report.get("repo_attribution_windows", {})
    attribution_7d = windows.get("7d", {})
    lines.extend(["", "repo attribution (last 7d):"])
    if attribution_7d:
        for agent, row in attribution_7d.items():
            lines.append(
                f"  {agent:25s} {row.get('percent'):5.1f}% "
                f"({row.get('attributed')}/{row.get('project_total', row.get('total'))} project, "
                f"{row.get('general_workspace', 0)} general)"
            )
    else:
        lines.append("  none")

    cwd_windows = report.get("top_unattributed_cwds", {})
    for label in ("24h", "7d"):
        samples = cwd_windows.get(label, [])
        lines.extend(["", f"top unattributed cwd/workspace samples (last {label}):"])
        if samples:
            for row in samples[:10]:
                lines.append(
                    f"  {row.get('agent_id')}: {row.get('cwd')} "
                    f"({row.get('count')})"
                )
        else:
            lines.append("  none")

    general_windows = report.get("general_workspace_cwds", {})
    for label in ("24h", "7d"):
        samples = general_windows.get(label, [])
        lines.extend(["", f"general workspace cwd samples (last {label}):"])
        if samples:
            for row in samples[:10]:
                lines.append(
                    f"  {row.get('agent_id')}: {row.get('cwd')} "
                    f"({row.get('count')})"
                )
        else:
            lines.append("  none")

    gap_windows = report.get("claude_attribution_gap_categories", {})
    category_labels = {
        "missing_producer_metadata": "missing producer metadata",
        "parser_collector_drift": "parser/collector drift",
        "general_context_activity": "general-context activity",
        "genuine_unknown": "genuine unknown",
    }
    for label in ("24h", "7d"):
        agents = gap_windows.get(label, {})
        lines.extend(["", f"Claude attribution gap categories (last {label}):"])
        if not agents:
            lines.append("  none")
            continue
        for agent, categories in agents.items():
            lines.append(f"  {agent}:")
            for category, row in categories.items():
                pretty = category_labels.get(category, category)
                samples = row.get("samples") or []
                sample_text = ", ".join(
                    f"{sample.get('cwd')} ({sample.get('count')})"
                    for sample in samples[:3]
                )
                suffix = f" — {sample_text}" if sample_text else ""
                lines.append(f"    {pretty}: {row.get('count', 0)}{suffix}")

    pending_by_source = report.get("pending_incoming_by_source", {})
    lines.extend(["", "pending incoming jsonl by source:"])
    if pending_by_source:
        for source, row in pending_by_source.items():
            lines.append(
                f"  {source:25s} count={row.get('count', 0)} "
                f"oldest_age={row.get('oldest_age_human', '-')} "
                f"oldest_file={row.get('oldest_file', '-')}"
            )
        lines.append(
            "  status: destination watcher bottleneck likely if shipper logs show "
            "successful rsync while these root-level files keep aging."
        )
    else:
        lines.append("  none")

    incoming = report.get("unprocessed_incoming", [])
    lines.extend(["", "unprocessed incoming jsonl:"])
    if incoming:
        for rel in incoming[:50]:
            lines.append(f"  {rel}")
        if len(incoming) > 50:
            lines.append(f"  ... {len(incoming) - 50} more")
    else:
        lines.append("  none")

    warnings = report.get("warnings", [])
    if warnings:
        lines.extend(["", "warnings:"])
        lines.extend(f"  ⚠ {w}" for w in warnings)
    return "\n".join(lines)


def audit_lakehouse(
    *,
    parquet_dir: Path,
    duckdb_path: Path,
    incoming_dir: Optional[Path] = None,
    drift_threshold: float = 0.01,
) -> dict:
    """Return a dict report. Never raises — failures show up as warnings."""
    parquet_dir = Path(parquet_dir)
    duckdb_path = Path(duckdb_path)
    warnings: list[str] = []

    con = open_duckdb_connection(duckdb_path, read_only=True, role="diagnostic")
    try:
        # `id` is present in both the new ingest schema and the legacy
        # CSV-derived migration parquet; `dedup_key` only in the former.
        agent_total = _safe_count(
            con, "SELECT count(*) FROM agent_events WHERE id IS NOT NULL"
        )
        spans_total = _safe_count(
            con,
            """
            SELECT count(*) FROM spans
            WHERE span_id IS NOT NULL
              AND date >= strftime(current_date - INTERVAL '32 days', '%Y-%m-%d')
              AND date <> '_seed'
            """,
        )
        sessions_total = _safe_count(
            con,
            "SELECT count(DISTINCT session_id) FROM agent_events WHERE id IS NOT NULL",
        )
        tasks_total = _safe_count(con, "SELECT count(*) FROM tasks")
        summaries_total = _safe_count(con, "SELECT count(*) FROM session_summaries")

        # Per-(date, agent_id) breakdown
        by_partition: dict[tuple[str, str], int] = {}
        try:
            cur = con.execute("""SELECT COALESCE(NULLIF(CAST(date AS VARCHAR), '_seed'),
                                   strftime(TRY_CAST(timestamp AS TIMESTAMP), '%Y-%m-%d')),
                          agent_id, count(*)
                   FROM agent_events
                   WHERE id IS NOT NULL
                   GROUP BY 1, 2""")
            for date, agent_id, n in cur.fetchall():
                if not date:
                    continue
                by_partition[(date, agent_id)] = int(n)
        except duckdb.Error as e:
            warnings.append(f"agent_events partition count failed: {e}")
    finally:
        con.close()

    processed = _per_host_processed(incoming_dir) if incoming_dir else {}

    # Drift detection: if a host has many processed files but the lakehouse
    # has zero rows for that agent, flag it. (An exact 1:1 file→row count is
    # not meaningful — JSONL files have variable event counts. We use a
    # crude "processed > 10 and rows == 0" floor instead.)
    if processed:
        rows_per_host: dict[str, int] = {}
        for (_, agent_id), n in by_partition.items():
            if not agent_id:
                continue
            rows_per_host[agent_id] = rows_per_host.get(agent_id, 0) + n
        for host, file_count in processed.items():
            # incoming dir name matches agent_id directly (e.g. "macmini-claude")
            row_count = rows_per_host.get(host, 0)
            if file_count >= 10 and row_count == 0:
                warnings.append(
                    f"host {host}: {file_count} processed files but 0 rows in lakehouse — pipeline may be broken"
                )
            elif file_count >= 10 and row_count < file_count * (1 - drift_threshold):
                warnings.append(
                    f"host {host}: drift {row_count}/{file_count} (>{int(drift_threshold * 100)}%)"
                )

    return {
        "agent_events_total": agent_total,
        "agent_events_by_partition": by_partition,
        "spans_total": spans_total,
        "sessions_total": sessions_total,
        "tasks_total": tasks_total,
        "summaries_total": summaries_total,
        "processed_files": processed,
        "warnings": warnings,
    }
