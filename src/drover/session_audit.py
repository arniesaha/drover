"""Read-only session consistency audit helpers.

The ``sessions`` relation is intended to be derived state: a DuckDB view over
``agent_events``. Session summaries are derived memory in the PostgreSQL
control store (#480), so summary coverage compares DuckDB's event session ids
with the summarized ids read from there. This module never repairs or
backfills data; it only reports drift and flags legacy base-table states
loudly so operators can take an explicit, backed-up remediation path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from drover.event_identity import AgentEventScan, canonical_agent_events_cte

LEGACY_SESSIONS_REMEDIATION = (
    "Back up the DuckDB file before making changes. The sessions relation is a "
    "legacy base table, but Drover expects sessions to be a view derived from "
    "agent_events. Rebuild it only from a reviewed maintenance "
    "window (for example by running schema bootstrap after the backup) and "
    "re-run `drover-server audit-sessions --json` to confirm zero drift."
)

MEMORY_UNAVAILABLE_WARNING = (
    "session summaries live in the PostgreSQL control store, which this hub "
    "does not have; summary coverage was not audited"
)

_ZERO_DRIFT_FIELDS = (
    "event_sessions_missing_session_row",
    "session_rows_without_events",
    "event_count_mismatches",
    "summaries_without_events",
)
_SUMMARY_DRIFT_FIELDS = ("event_sessions_without_summary", "summaries_without_events")


def _safe_scalar(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    *,
    warnings: list[str],
    label: str,
) -> int | None:
    try:
        row = con.execute(sql).fetchone()
    except duckdb.Error as exc:
        warnings.append(f"{label} query failed: {exc}")
        return None
    return int(row[0]) if row and row[0] is not None else 0


_EVENT_SESSION_IDS_SQL = (
    "SELECT DISTINCT session_id FROM {events} WHERE session_id IS NOT NULL"
)


def summarized_session_ids(
    memory_store_path: Path | str | None, *, warnings: list[str]
) -> set[str] | None:
    """Every session with a final summary, or None when that is unknowable.

    None means no PostgreSQL memory store (or an unreadable one); an empty set
    means the store has no summaries yet.
    """
    from drover.server.ledger import memory_store_available

    if memory_store_path is None or not memory_store_available(memory_store_path):
        warnings.append(MEMORY_UNAVAILABLE_WARNING)
        return None
    try:
        from drover.server.memory_store import CANONICAL_MEMORY, MemoryRepository

        with MemoryRepository(memory_store_path).connection() as pg:
            rows = pg.execute(
                f"SELECT session_id FROM session_memory WHERE phase = 'final' AND {CANONICAL_MEMORY}"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - an audit reports, never raises
        warnings.append(f"summarized session ids query failed: {exc}")
        return None
    return {str(row[0]) for row in rows if row[0] is not None}


def _session_set_metrics(
    con: duckdb.DuckDBPyConnection,
    *,
    warnings: list[str],
    events: str,
    summary_ids: set[str] | None,
) -> tuple[int | None, int | None, int | None]:
    """Return the three session-set metrics from a single ``events`` scan.

    The event side is one DISTINCT scan of ``events`` (asking per metric used
    to mean three full scans of the parquet tree, a large share of the
    /metrics cost, #78). The summary side comes from PostgreSQL, so the two
    id sets are compared here rather than joined in SQL.
    """
    try:
        event_ids = {
            str(row[0])
            for row in con.execute(
                _EVENT_SESSION_IDS_SQL.format(events=events)
            ).fetchall()
        }
    except duckdb.Error as exc:
        warnings.append(f"event_sessions query failed: {exc}")
        return (None, None, None)
    if summary_ids is None:
        return (len(event_ids), None, None)
    return (
        len(event_ids),
        len(event_ids - summary_ids),
        len(summary_ids - event_ids),
    )


def _relation_type(con: duckdb.DuckDBPyConnection, name: str) -> str | None:
    row = con.execute(
        """
        SELECT table_type
        FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_name = ?
        ORDER BY CASE table_type WHEN 'VIEW' THEN 0 WHEN 'BASE TABLE' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        [name],
    ).fetchone()
    return str(row[0]) if row else None


def _has_column(con: duckdb.DuckDBPyConnection, relation: str, column: str) -> bool:
    row = con.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = ?
          AND column_name = ?
        LIMIT 1
        """,
        [relation, column],
    ).fetchone()
    return row is not None


def _event_count_source(con: duckdb.DuckDBPyConnection) -> tuple[str, str]:
    if _has_column(con, "agent_events", "dedup_key"):
        return f"WITH {canonical_agent_events_cte()}", "canonical_agent_events"
    return "", "agent_events"


def audit_session_consistency(
    con: duckdb.DuckDBPyConnection,
    *,
    duckdb_path: Path | str | None = None,
    include_expensive_checks: bool = True,
    scan: AgentEventScan | None = None,
    memory_store_path: Path | str | None = None,
) -> dict[str, Any]:
    """Return a read-only session/session-summary consistency report.

    The caller owns the DuckDB connection. All queries are ``SELECT`` metadata or
    count queries; this function intentionally performs no schema bootstrap,
    repair, backfill, DDL, or writes.

    Summarized session ids are read from the PostgreSQL control store
    registered for ``memory_store_path`` (default ``duckdb_path``). Without
    one, the summary fields are None, ``summaries_available`` is False, and
    they do not count towards drift.

    ``scan`` is the caller's shared whole-history pass over ``agent_events``
    (see ``drover.event_identity``). When it carries ``session_id`` the
    session-set queries read it instead of re-scanning the parquet tree; the
    reported numbers are the same either way. ``event_count_mismatches`` still
    reads ``agent_events`` because canonical dedupe ranks on columns the pass
    does not carry.
    """
    events = (
        scan.relation if scan is not None and scan.has_session_id else "agent_events"
    )

    warnings: list[str] = []
    report: dict[str, Any] = {
        "duckdb_path": str(duckdb_path) if duckdb_path is not None else None,
        "sessions_relation_type": None,
        "status": "unknown",
        "is_clean": False,
        "event_sessions": None,
        "sessions_rows": None,
        "event_sessions_missing_session_row": None,
        "session_rows_without_events": None,
        "event_count_mismatches": None,
        "event_sessions_without_summary": None,
        "summaries_without_events": None,
        "summaries_available": False,
        "warnings": warnings,
        "remediation": None,
    }

    agent_events_type = _relation_type(con, "agent_events")
    sessions_type = _relation_type(con, "sessions")
    report["sessions_relation_type"] = sessions_type

    if agent_events_type is None:
        report["status"] = "missing_agent_events"
        warnings.append(
            "agent_events relation is missing; cannot audit session consistency"
        )
        return report
    if sessions_type is None:
        report["status"] = "missing_sessions"
        warnings.append("sessions relation is missing; expected a DuckDB VIEW")
        return report

    summary_ids = summarized_session_ids(
        memory_store_path if memory_store_path is not None else duckdb_path,
        warnings=warnings,
    )
    report["summaries_available"] = summary_ids is not None
    (
        event_sessions,
        event_sessions_without_summary,
        summaries_without_events,
    ) = _session_set_metrics(
        con, warnings=warnings, events=events, summary_ids=summary_ids
    )
    report["event_sessions"] = event_sessions
    if include_expensive_checks:
        report["sessions_rows"] = _safe_scalar(
            con,
            "SELECT count(*) FROM sessions WHERE session_id IS NOT NULL",
            warnings=warnings,
            label="sessions_rows",
        )
        report["event_sessions_missing_session_row"] = _safe_scalar(
            con,
            f"""
            SELECT count(*)
            FROM (SELECT DISTINCT session_id FROM {events} WHERE session_id IS NOT NULL) e
            LEFT JOIN (SELECT DISTINCT session_id FROM sessions WHERE session_id IS NOT NULL) s
              USING (session_id)
            WHERE s.session_id IS NULL
            """,
            warnings=warnings,
            label="event_sessions_missing_session_row",
        )
        report["session_rows_without_events"] = _safe_scalar(
            con,
            f"""
            SELECT count(*)
            FROM (SELECT DISTINCT session_id FROM sessions WHERE session_id IS NOT NULL) s
            LEFT JOIN (SELECT DISTINCT session_id FROM {events} WHERE session_id IS NOT NULL) e
              USING (session_id)
            WHERE e.session_id IS NULL
            """,
            warnings=warnings,
            label="session_rows_without_events",
        )
    else:
        report["expensive_checks_skipped"] = True

    if include_expensive_checks and _has_column(con, "sessions", "event_count"):
        event_count_with, event_count_relation = _event_count_source(con)
        report["event_count_mismatches"] = _safe_scalar(
            con,
            f"""
            {event_count_with}
            {"," if event_count_with else "WITH"} event_counts AS (
              SELECT session_id, count(*) AS event_count
              FROM {event_count_relation}
              WHERE session_id IS NOT NULL
              GROUP BY session_id
            )
            SELECT count(*)
            FROM event_counts e
            JOIN sessions s USING (session_id)
            WHERE s.event_count IS DISTINCT FROM e.event_count
            """,
            warnings=warnings,
            label="event_count_mismatches",
        )
    else:
        report["event_count_mismatches"] = None
        if include_expensive_checks:
            warnings.append(
                "sessions relation has no event_count column; cannot verify counts"
            )

    report["event_sessions_without_summary"] = event_sessions_without_summary
    report["summaries_without_events"] = summaries_without_events

    drift_fields = (
        _ZERO_DRIFT_FIELDS
        if include_expensive_checks
        else ("event_sessions_without_summary", "summaries_without_events")
    )
    if summary_ids is None:
        # No memory store is a known absence, not an unknown measurement.
        drift_fields = tuple(f for f in drift_fields if f not in _SUMMARY_DRIFT_FIELDS)
    drift_values = [report.get(field) for field in drift_fields]
    has_unknown = any(value is None for value in drift_values)
    has_drift = any((value or 0) != 0 for value in drift_values if value is not None)

    if sessions_type == "BASE TABLE":
        report["status"] = "legacy_base_table"
        report["remediation"] = LEGACY_SESSIONS_REMEDIATION
        warnings.append(
            "sessions is a legacy base table; expected DuckDB VIEW. "
            "No automatic repair was attempted."
        )
    elif sessions_type != "VIEW":
        report["status"] = "unexpected_sessions_relation_type"
        warnings.append(f"sessions relation has unexpected type {sessions_type!r}")
    elif has_unknown:
        report["status"] = "incomplete"
    elif has_drift:
        report["status"] = "drift"
        warnings.append(
            "session/summary drift detected; inspect audit-sessions JSON output"
        )
    else:
        report["status"] = "ok"
        report["is_clean"] = True

    return report


def audit_session_consistency_db(duckdb_path: Path | str) -> dict[str, Any]:
    """Open ``duckdb_path`` read-only and return a session consistency report.

    Summary coverage reads the PostgreSQL control store registered for the
    same path.
    """

    path = Path(duckdb_path)
    if not path.exists():
        return {
            "duckdb_path": str(path),
            "sessions_relation_type": None,
            "status": "missing_db",
            "is_clean": False,
            "event_sessions": None,
            "sessions_rows": None,
            "event_sessions_missing_session_row": None,
            "session_rows_without_events": None,
            "event_count_mismatches": None,
            "event_sessions_without_summary": None,
            "summaries_without_events": None,
            "summaries_available": False,
            "warnings": [f"DuckDB path does not exist: {path}"],
            "remediation": None,
        }
    con = duckdb.connect(str(path), read_only=True)
    try:
        return audit_session_consistency(con, duckdb_path=path)
    finally:
        con.close()


def format_session_audit(report: dict[str, Any]) -> str:
    """Format an ``audit_session_consistency`` report for humans."""

    lines = [
        "Drover session consistency audit",
        "=================================",
        f"db                : {report.get('duckdb_path') or '-'}",
        f"status            : {report.get('status')}",
        f"sessions relation : {report.get('sessions_relation_type') or 'missing'}",
        "",
        "counts:",
    ]
    for key in (
        "event_sessions",
        "sessions_rows",
        "event_sessions_missing_session_row",
        "session_rows_without_events",
        "event_count_mismatches",
        "event_sessions_without_summary",
        "summaries_without_events",
    ):
        value = report.get(key)
        lines.append(f"  {key:34s} {'unknown' if value is None else value}")

    remediation = report.get("remediation")
    if remediation:
        lines.extend(["", "remediation:", f"  {remediation}"])

    warnings = report.get("warnings") or []
    if warnings:
        lines.extend(["", "warnings:"])
        lines.extend(f"  ⚠ {warning}" for warning in warnings)
    return "\n".join(lines)
