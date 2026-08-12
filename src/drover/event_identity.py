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
from dataclasses import dataclass
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


#: Columns the shared pass carries. Between them they answer every
#: whole-history metric the runtime audit reports: the row count, the distinct
#: session-id set, and the duplicate ``id`` / ``dedup_key`` rollups.
_SCAN_COLUMNS = ("id", "dedup_key", "session_id")


@dataclass(frozen=True)
class AgentEventScan:
    """One materialised pass over the whole ``agent_events`` history.

    ``agent_events`` is a view over a multi-thousand file parquet tree, so each
    statement naming it without a ``date >=`` bound re-reads the entire tree.
    Profiled against a 6,876-file / 2.23M-row store at ``threads=1``, the three
    whole-history statements the audit issued cost 1.61s (row count), 2.37s
    (session-set metrics) and 2.71s (duplicate identity) out of an 11.4s audit
    -- while needing only three columns between them (#78).

    ``relation`` names a temp table holding those three columns pre-aggregated
    with a ``rows`` count, so consumers sum counts instead of counting rows.
    """

    relation: str
    total_rows: int
    has_id: bool
    has_dedup_key: bool
    has_session_id: bool

    @property
    def row_count_expr(self) -> str:
        """Aggregate that yields a row count over the pre-aggregated pass."""
        return "sum(rows)"


def scan_agent_events_once(
    con: duckdb.DuckDBPyConnection,
    *,
    source: str = "agent_events",
    name: str = "agent_event_scan",
) -> AgentEventScan | None:
    """Materialise the shared whole-history pass, or ``None`` if unavailable.

    The temp table is connection-local: it never touches the database file, so
    read-only audit semantics hold. Callers that get ``None`` -- an unreadable
    relation, or a DuckDB that refused the pass -- must fall back to reading
    ``source`` directly, which is what every consumer did before #78.
    """
    name = _validate_identifier(name, label="scan relation")
    source = _validate_identifier(source, label="source relation")
    columns = _relation_columns(con, source)
    if columns is None:
        return None
    present = [column for column in _SCAN_COLUMNS if column in columns]
    projection = ", ".join(present)
    # GROUP BY collapses repeated key triples, which keeps the pass smaller
    # than the source and hands the identity rollup its aggregation for free.
    # With no key columns at all there is nothing to group and the pass
    # degenerates to the row count the audit still needs.
    body = (
        f"SELECT {projection}, count(*) AS rows FROM {source} GROUP BY {projection}"
        if present
        else f"SELECT count(*) AS rows FROM {source}"
    )
    try:
        con.execute(f"CREATE OR REPLACE TEMP TABLE {name} AS {body}")
        row = con.execute(f"SELECT COALESCE(sum(rows), 0) FROM {name}").fetchone()
    except duckdb.Error:
        return None
    return AgentEventScan(
        relation=name,
        total_rows=int(row[0]) if row and row[0] is not None else 0,
        has_id="id" in present,
        has_dedup_key="dedup_key" in present,
        has_session_id="session_id" in present,
    )


def _identity_metrics_sql(
    *,
    has_id: bool,
    has_dedup_key: bool,
    example_limit: int,
    source: str = "agent_events",
    row_count: str = "count(*)",
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

    ``source`` and ``row_count`` let the same query run over the shared
    ``AgentEventScan`` pass, where the rows are already grouped and a row count
    is ``sum(rows)`` rather than ``count(*)``.
    """
    ctes: list[str] = []
    selects: list[str] = []
    paired = has_id and has_dedup_key
    if paired:
        ctes.append(f"""
            identity_pairs AS MATERIALIZED (
              SELECT id, dedup_key, {row_count} AS rows
              FROM {source}
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
            ctes.append(f"""
            duplicate_ids AS MATERIALIZED (
              SELECT id, {row_count} AS rows, 0 AS dedup_keys
              FROM {source}
              WHERE id IS NOT NULL
              GROUP BY id
              HAVING {row_count} > 1
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
            ctes.append(f"""
            duplicate_dedup_keys AS MATERIALIZED (
              SELECT dedup_key, {row_count} AS rows
              FROM {source}
              WHERE dedup_key IS NOT NULL
              GROUP BY dedup_key
              HAVING {row_count} > 1
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
    con: duckdb.DuckDBPyConnection,
    *,
    example_limit: int = 5,
    scan: AgentEventScan | None = None,
) -> dict[str, Any]:
    """Return read-only duplicate identity metrics for ``agent_events``.

    The ``*_rows`` values count excess duplicate rows beyond the first row in
    each duplicate group, matching the lakehouse data-quality audit convention.
    The report intentionally tracks both historical/source ``id`` collisions and
    canonical ``dedup_key`` collisions. Consumers that need logical event
    uniqueness should dedupe on ``dedup_key`` per ``EVENT_IDENTITY_SEMANTICS``.

    Pass ``scan`` to read the caller's shared whole-history pass instead of
    re-reading the parquet tree. The metrics are identical either way; the
    pass is only a cheaper place to read the same three columns from.
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

    if scan is not None:
        source, row_count = scan.relation, scan.row_count_expr
        has_id, has_dedup_key = scan.has_id, scan.has_dedup_key
    else:
        source, row_count = "agent_events", "count(*)"
        columns = _relation_columns(con, source)
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
                source=source,
                row_count=row_count,
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
