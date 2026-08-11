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


def _relation_columns(con: duckdb.DuckDBPyConnection, relation: str) -> set[str] | None:
    """Return ``relation``'s column names, or ``None`` if it is unreadable.

    Catalog metadata first: ``agent_events`` is a view over a multi-thousand
    file parquet glob, so binding it costs about a second even for a zero-row
    probe. The direct probe stays as the fallback for relations the catalog
    does not describe, and is what distinguishes "missing" from "no such
    column" (see #78).
    """
    try:
        rows = con.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = ?
            """,
            [relation],
        ).fetchall()
    except duckdb.Error:
        rows = []
    if rows:
        return {str(row[0]) for row in rows}
    try:
        desc = con.execute(f"SELECT * FROM {relation} LIMIT 0").description or []
    except duckdb.Error:
        return None
    return {str(col[0]) for col in desc}


def _identity_metrics_sql(
    *, has_id: bool, has_dedup_key: bool, example_limit: int
) -> str:
    """Build the single-pass duplicate-identity query.

    Every statement against ``agent_events`` reads the whole historical parquet
    tree, so this deliberately answers all of the identity metrics from one
    statement instead of one statement per metric. The CTEs are ``MATERIALIZED``
    so that the several scalar subqueries reading them do not re-run the
    aggregation.

    When both columns are present the counts are rolled up from one
    ``(id, dedup_key)`` grouping rather than grouping ``agent_events`` twice.
    That also turns the per-id ``count(DISTINCT dedup_key)`` into a distinct
    count over an already-deduplicated relation, which was the single most
    expensive aggregate in the audit (see #78).
    """
    ctes: list[str] = []
    selects: list[str] = []
    paired = has_id and has_dedup_key
    if paired:
        ctes.append("""
            identity_pairs AS MATERIALIZED (
              SELECT id, dedup_key, count(*) AS rows
              FROM agent_events
              WHERE id IS NOT NULL OR dedup_key IS NOT NULL
              GROUP BY id, dedup_key
            )""")
    if has_id:
        if paired:
            ctes.append("""
            duplicate_ids AS MATERIALIZED (
              SELECT id, sum(rows) AS rows, count(DISTINCT dedup_key) AS dedup_keys
              FROM identity_pairs
              WHERE id IS NOT NULL
              GROUP BY id
              HAVING sum(rows) > 1
            )""")
        else:
            ctes.append("""
            duplicate_ids AS MATERIALIZED (
              SELECT id, count(*) AS rows, 0 AS dedup_keys
              FROM agent_events
              WHERE id IS NOT NULL
              GROUP BY id
              HAVING count(*) > 1
            )""")
        selects.extend(
            [
                "(SELECT count(*) FROM duplicate_ids) AS duplicate_id_values",
                "(SELECT COALESCE(sum(rows - 1), 0) FROM duplicate_ids)"
                " AS duplicate_id_rows",
                f"""(
              SELECT list(
                {{'id': id, 'rows': rows, 'dedup_keys': dedup_keys}}
                ORDER BY rows DESC, id
              )
              FROM (
                SELECT * FROM duplicate_ids ORDER BY rows DESC, id LIMIT {example_limit}
              )
            ) AS duplicate_id_examples""",
            ]
        )
    else:
        selects.extend(
            [
                "0 AS duplicate_id_values",
                "0 AS duplicate_id_rows",
                "NULL AS duplicate_id_examples",
            ]
        )
    if has_dedup_key:
        if paired:
            ctes.append("""
            duplicate_dedup_keys AS MATERIALIZED (
              SELECT dedup_key, sum(rows) AS rows
              FROM identity_pairs
              WHERE dedup_key IS NOT NULL
              GROUP BY dedup_key
              HAVING sum(rows) > 1
            )""")
        else:
            ctes.append("""
            duplicate_dedup_keys AS MATERIALIZED (
              SELECT dedup_key, count(*) AS rows
              FROM agent_events
              WHERE dedup_key IS NOT NULL
              GROUP BY dedup_key
              HAVING count(*) > 1
            )""")
        selects.extend(
            [
                "(SELECT count(*) FROM duplicate_dedup_keys)"
                " AS duplicate_dedup_key_values",
                "(SELECT COALESCE(sum(rows - 1), 0) FROM duplicate_dedup_keys)"
                " AS duplicate_dedup_key_rows",
            ]
        )
    else:
        selects.extend(
            [
                "0 AS duplicate_dedup_key_values",
                "0 AS duplicate_dedup_key_rows",
            ]
        )
    with_clause = "WITH " + ",".join(ctes) if ctes else ""
    return f"{with_clause}\nSELECT {', '.join(selects)}"


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

    columns = _relation_columns(con, "agent_events")
    if columns is None:
        report["status"] = "missing"
        report["error"] = "agent_events relation is not readable"
        return report

    has_id = "id" in columns
    has_dedup_key = "dedup_key" in columns
    try:
        row = con.execute(
            _identity_metrics_sql(
                has_id=has_id,
                has_dedup_key=has_dedup_key,
                example_limit=max(0, int(example_limit)),
            )
        ).fetchone()
    except duckdb.Error as e:
        report["status"] = "missing"
        report["error"] = str(e)
        return report

    if row is None:
        row = (0, 0, None, 0, 0)

    if has_id:
        report["duplicate_id_values"] = int(row[0] or 0)
        report["duplicate_id_rows"] = int(row[1] or 0)
        report["duplicate_id_examples"] = [
            {
                "id": str(example["id"]),
                "rows": int(example["rows"]),
                "dedup_keys": int(example["dedup_keys"]),
            }
            for example in (row[2] or [])
        ]
    else:
        report["status"] = "missing_id_column"

    if has_dedup_key:
        report["duplicate_dedup_key_values"] = int(row[3] or 0)
        report["duplicate_dedup_key_rows"] = int(row[4] or 0)
        if report["duplicate_dedup_key_values"]:
            report["status"] = "duplicate_dedup_key"
    else:
        report["status"] = "missing_dedup_key_column"

    return report
