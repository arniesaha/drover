"""Canonical identity semantics for ``agent_events`` rows.

``agent_events.id`` is an upstream/event-source identifier. It is useful for
traceability, but it is not guaranteed to be globally unique in the historical
lakehouse because legacy imports can contain timestamp-normalization variants of
the same event.

The canonical event identity for ingestion and downstream de-duplication is
``dedup_key``. It is derived from stable business fields by
``drover.dedup.make_dedup_key`` and should be used when a query needs one logical
row per event. Runtime audit still reports duplicate ``id`` values as a data
quality signal, but duplicate ``id`` values alone do not define duplicate logical
events.
"""

from __future__ import annotations

import re
from typing import Any

import duckdb

CANONICAL_EVENT_IDENTITY = "dedup_key"
EVENT_IDENTITY_SEMANTICS = (
    "agent_events.id is source/trace identity and may be duplicated in legacy "
    "lakehouse data; agent_events.dedup_key is the canonical logical event "
    "identity for ingestion and downstream dedupe."
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(value: str, *, label: str) -> str:
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(f"invalid SQL identifier for {label}: {value!r}")
    return value


def canonical_agent_events_cte(
    *,
    name: str = "canonical_agent_events",
    source: str = "agent_events",
) -> str:
    """Return a DuckDB CTE that exposes one row per logical agent event.

    The raw ``agent_events`` relation intentionally remains a physical/audit
    surface. Downstream readers that need logical event uniqueness should query
    this CTE, which collapses repeated non-null ``dedup_key`` values while
    preserving rows that do not have a canonical key.
    """
    name = _validate_identifier(name, label="cte name")
    source = _validate_identifier(source, label="source relation")
    return f"""
{name} AS (
  SELECT * EXCLUDE (_drover_identity_rank)
  FROM (
    SELECT ae.*,
           row_number() OVER (
             PARTITION BY ae.dedup_key
             ORDER BY (ae.repo_owner IS NOT NULL AND ae.repo_name IS NOT NULL) DESC,
                      TRY_CAST(ae.timestamp AS TIMESTAMPTZ) DESC NULLS LAST,
                      ae.id DESC NULLS LAST
           ) AS _drover_identity_rank
      FROM {source} ae
  )
  WHERE dedup_key IS NULL OR _drover_identity_rank = 1
)
""".strip()


def _has_column(con: duckdb.DuckDBPyConnection, relation: str, column: str) -> bool:
    try:
        row = con.execute(
            """
            SELECT count(*)
            FROM information_schema.columns
            WHERE table_name = ? AND column_name = ?
            """,
            [relation, column],
        ).fetchone()
        return bool(row and row[0])
    except duckdb.Error:
        try:
            desc = con.execute(f"SELECT * FROM {relation} LIMIT 0").description or []
        except duckdb.Error:
            return False
        return column in {str(col[0]) for col in desc}


def _duplicate_metrics(con: duckdb.DuckDBPyConnection, column: str) -> tuple[int, int]:
    row = con.execute(f"""
        SELECT count(*) AS duplicate_values,
               COALESCE(sum(rows - 1), 0) AS duplicate_rows
        FROM (
            SELECT {column}, count(*) AS rows
            FROM agent_events
            WHERE {column} IS NOT NULL
            GROUP BY {column}
            HAVING count(*) > 1
        )
        """).fetchone()
    return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)


def audit_agent_event_identity(
    con: duckdb.DuckDBPyConnection, *, example_limit: int = 5
) -> dict[str, Any]:
    """Return read-only duplicate identity metrics for ``agent_events``.

    The ``*_rows`` values count excess duplicate rows beyond the first row in
    each duplicate group, matching the lakehouse data-quality audit convention.
    The report intentionally tracks both historical/source ``id`` collisions and
    canonical ``dedup_key`` collisions. Consumers that need logical event
    uniqueness should dedupe on ``dedup_key`` per ``EVENT_IDENTITY_SEMANTICS``.
    """
    report: dict[str, Any] = {
        "status": "ok",
        "canonical_semantics": CANONICAL_EVENT_IDENTITY,
        "semantics": EVENT_IDENTITY_SEMANTICS,
        "source_id_context": "source/provenance only; not canonical health",
        "duplicate_id_values": 0,
        "duplicate_id_rows": 0,
        "duplicate_dedup_key_values": 0,
        "duplicate_dedup_key_rows": 0,
        "duplicate_id_examples": [],
    }

    try:
        con.execute("SELECT 1 FROM agent_events LIMIT 0")
    except duckdb.Error as e:
        report["status"] = "missing"
        report["error"] = str(e)
        return report

    if _has_column(con, "agent_events", "id"):
        values, rows = _duplicate_metrics(con, "id")
        report["duplicate_id_values"] = values
        report["duplicate_id_rows"] = rows
        examples = con.execute(
            (
                """
            SELECT id, count(*) AS rows, count(DISTINCT dedup_key) AS dedup_keys
            FROM agent_events
            WHERE id IS NOT NULL
            GROUP BY id
            HAVING count(*) > 1
            ORDER BY rows DESC, id
            LIMIT ?
            """
                if _has_column(con, "agent_events", "dedup_key")
                else """
            SELECT id, count(*) AS rows, 0 AS dedup_keys
            FROM agent_events
            WHERE id IS NOT NULL
            GROUP BY id
            HAVING count(*) > 1
            ORDER BY rows DESC, id
            LIMIT ?
            """
            ),
            [example_limit],
        ).fetchall()
        report["duplicate_id_examples"] = [
            {"id": str(event_id), "rows": int(n), "dedup_keys": int(dedup_keys)}
            for event_id, n, dedup_keys in examples
        ]
    else:
        report["status"] = "missing_id_column"

    if _has_column(con, "agent_events", "dedup_key"):
        values, rows = _duplicate_metrics(con, "dedup_key")
        report["duplicate_dedup_key_values"] = values
        report["duplicate_dedup_key_rows"] = rows
        if values:
            report["status"] = "duplicate_dedup_key"
    else:
        report["status"] = "missing_dedup_key_column"

    return report
