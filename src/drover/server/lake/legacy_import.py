"""Selective, idempotent imports from the retained legacy parquet archive."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .fence import drained_mutation
from .rebuild import EVENT_SCHEMA, row_hash_expression
from .rebuild_worker import POLICY_SCHEMA
from .runtime import LakeError, LakeSpec, create_table, lake_connection, literal
from .serving_proof import write_proof

IMPORT_WATERMARK_SCHEMA = {"partition_date": "VARCHAR"}


def _require_selection(since: str | None, session_id: str | None) -> str:
    if bool(since) == bool(session_id):
        raise LakeError("lake_import_requires_exactly_one_selector")
    if since is not None:
        try:
            return date.fromisoformat(since).isoformat()
        except ValueError:
            raise LakeError("lake_import_since_invalid") from None
    assert session_id is not None
    if not session_id.strip():
        raise LakeError("lake_import_session_invalid")
    return session_id


def _table_names(con) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_catalog='lake' AND table_schema='main'"
        ).fetchall()
    }


def _legacy_projection(con) -> str:
    columns = {row[0] for row in con.execute("DESCRIBE legacy_source").fetchall()}
    if not {"id", "session_id", "timestamp", "dedup_key", "date"} <= columns:
        raise LakeError("lake_import_source_schema_invalid")
    fields = []
    for name, typ in EVENT_SCHEMA.items():
        value = f'"{name}"' if name in columns else "NULL"
        fields.append(f'CAST({value} AS {typ}) AS "{name}"')
    return ", ".join(fields)


def _publish_proof(spec: LakeSpec, con, fence) -> int:
    """Make the import snapshot the new trusted serving baseline.

    The original offline verification hashes remain unchanged: this operation
    only admits rows from an explicitly selected retained archive and then
    advances the catalog snapshot under the same exclusive fence.
    """
    verification = spec.data_root / "verification"
    try:
        import json

        tables = json.loads((verification / "last-verify.json").read_text())["tables"]
    except (OSError, ValueError, KeyError, TypeError):
        raise LakeError("lake_verification_baseline_missing") from None
    snapshot = con.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[
        0
    ]
    if snapshot is None:
        raise LakeError("lake_import_snapshot_missing")
    write_proof(spec, con, tables, fence=fence, snapshot=snapshot)
    return snapshot


def run_import(
    spec: LakeSpec,
    legacy_root: Path,
    *,
    since: str | None = None,
    session_id: str | None = None,
) -> dict[str, int | str | None]:
    """Import one date range or one session from a legacy parquet root.

    ``--since`` advances a single lower-bound watermark. A targeted historical
    session intentionally does not move it: importing one old session must not
    assert that every older session is available in the lake.
    """
    selection = _require_selection(since, session_id)
    root = Path(legacy_root).resolve()
    source = root / "agent_events"
    if not source.is_dir():
        raise LakeError("lake_import_source_missing")

    with (
        drained_mutation(spec.dsn()) as fence,
        lake_connection(spec, read_only=False) as con,
    ):
        tables = _table_names(con)
        if "agent_events" not in tables:
            raise LakeError("lake_import_events_table_missing")
        if "import_watermark" not in tables:
            create_table(con, "import_watermark", IMPORT_WATERMARK_SCHEMA)

        con.execute(
            "CREATE TEMP VIEW legacy_source AS SELECT * FROM "
            "read_parquet("
            + literal(str(source / "**/*.parquet"))
            + ", union_by_name=true, hive_partitioning=true)"
        )
        projection = _legacy_projection(con)
        predicate = (
            "date >= " + literal(selection)
            if since is not None
            else "session_id = " + literal(selection)
        )
        con.execute(
            "CREATE TEMP TABLE import_candidates AS "
            "SELECT * EXCLUDE (_import_row), "
            "row_number() OVER () - 1 AS _import_row, "
            + row_hash_expression(POLICY_SCHEMA)
            + " AS _row_sha256 FROM (SELECT "
            + projection
            + ", CASE WHEN dedup_key IS NULL THEN 'legacy_null' ELSE 'original' END "
            "AS dedup_key_source, dedup_key AS _source_dedup_key, "
            + literal(f"legacy-import:{selection}")
            + " AS _import_file, 0 AS _import_row FROM legacy_source WHERE "
            + predicate
            + ")"
        )
        con.execute(
            "CREATE TEMP TABLE canonical_import AS "
            "SELECT * EXCLUDE (rank) FROM (SELECT *, row_number() OVER "
            "(PARTITION BY COALESCE('key:' || dedup_key, 'row:' || _row_sha256) "
            "ORDER BY _import_file, _import_row) AS rank FROM import_candidates) "
            "WHERE rank=1"
        )
        selected = con.execute("SELECT count(*) FROM canonical_import").fetchone()[0]
        missing = (
            "SELECT * FROM canonical_import source WHERE NOT EXISTS ("
            "SELECT 1 FROM lake.agent_events target WHERE "
            "(source.dedup_key IS NOT NULL AND target.dedup_key = source.dedup_key) "
            "OR (source.dedup_key IS NULL "
            "AND target._row_sha256 = source._row_sha256))"
        )
        inserted = con.execute("SELECT count(*) FROM (" + missing + ")").fetchone()[0]
        con.execute("INSERT INTO lake.agent_events BY NAME " + missing)

        if since is not None:
            current = con.execute(
                "SELECT min(partition_date) FROM lake.import_watermark"
            ).fetchone()[0]
            if current is None or selection < current:
                con.execute("DELETE FROM lake.import_watermark")
                con.execute("INSERT INTO lake.import_watermark VALUES (?)", [selection])

        fence.check()
        snapshot = _publish_proof(spec, con, fence)
    return {
        "selector": "since" if since is not None else "session",
        "selection": selection,
        "selected_rows": selected,
        "inserted_rows": inserted,
        "snapshot": snapshot,
    }
