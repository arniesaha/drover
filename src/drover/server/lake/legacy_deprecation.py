"""Fenced retirement of unreceipted rows written by the retired importer."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from time import monotonic
from uuid import uuid4

from .activity_daily import refresh_activity_daily
from .fence import drained_mutation
from .maintenance import MAINTENANCE_AUTHOR, _append_receipt, _baseline_snapshot
from .partition_rebuild import verify_partitioned
from .runtime import LakeError, LakeSpec, lake_connection

DEPRECATION_RECORDS = "legacy-deprecations.json"
LEGACY_PREFIX = "legacy-import:%"
SELECTION_RULE = (
    "_import_file LIKE 'legacy-import:%' AND no exporter receipt/version evidence; "
    "import_watermark is retired importer state"
)


def _table_names(con) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_catalog='lake' AND table_schema='main'"
        ).fetchall()
    }


def _per_day(con, sql: str, parameters=()) -> list[dict]:
    return [
        {"day": row[0] or "<unknown>", "rows": int(row[1])}
        for row in con.execute(sql, parameters).fetchall()
    ]


def _selection_plan(con) -> dict:
    """Return only rows whose legacy provenance is not independently attested."""
    names = _table_names(con)
    tables: dict[str, list[dict]] = {}
    affected_days: set[str] = set()

    def add(name: str, sql: str, parameters=()):
        if name not in names:
            tables[name] = []
            return
        rows = _per_day(con, sql, parameters)
        tables[name] = rows
        affected_days.update(row["day"] for row in rows if row["day"] != "<unknown>")

    add(
        "agent_events",
        """SELECT date, count(*) FROM lake.agent_events event
           WHERE event._import_file LIKE ? AND NOT EXISTS (
             SELECT 1 FROM lake.export_event_versions version
             WHERE version._row_sha256=event._row_sha256
           ) GROUP BY 1 ORDER BY 1""",
        [LEGACY_PREFIX],
    )
    # Outbox records have no version row: their only exporter evidence is the
    # batch receipt named by an outbox import file.  A legacy import can never
    # satisfy that predicate, but spelling it out keeps the selection rule
    # conservative if an old catalog used a mixed filename.
    add(
        "control_outbox_batches",
        """SELECT CAST(created_at AS DATE)::VARCHAR, count(*)
           FROM lake.control_outbox_batches row
           WHERE row._import_file LIKE ? AND NOT EXISTS (
             SELECT 1 FROM lake.export_batch_receipts receipt
             WHERE receipt.batch_id=substr(row._import_file, 8)
           ) GROUP BY 1 ORDER BY 1""",
        [LEGACY_PREFIX],
    )
    # Provider observations are not exporter output, so no receipt/version can
    # establish provenance for a legacy-import row.
    add(
        "provider_usage_snapshots",
        """SELECT CAST(observed_at AS DATE)::VARCHAR, count(*)
           FROM lake.provider_usage_snapshots
           WHERE _import_file LIKE ? GROUP BY 1 ORDER BY 1""",
        [LEGACY_PREFIX],
    )
    # This table is solely the retired importer's cursor and has no row-level
    # lineage.  Removing it prevents history coverage from claiming that the
    # deprecated import is still part of the live lake.
    add(
        "import_watermark",
        "SELECT partition_date, count(*) FROM lake.import_watermark GROUP BY 1 ORDER BY 1",
    )
    return {
        "selection_rule": SELECTION_RULE,
        "tables": tables,
        "affected_days": sorted(affected_days),
        "rows": sum(row["rows"] for rows in tables.values() for row in rows),
    }


def deprecation_plan(spec: LakeSpec) -> dict:
    """Read-only inventory for the default operator dry-run."""
    with lake_connection(spec) as con:
        plan = _selection_plan(con)
    return {"dry_run": True, **plan}


def _append_deprecation_record(spec, record, *, fence) -> None:
    path = spec.data_root / "verification" / DEPRECATION_RECORDS
    try:
        records = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(records, list):
            raise ValueError()
    except (OSError, ValueError):
        raise LakeError("lake_deprecation_evidence_malformed") from None
    temporary = path.with_suffix(".pending")
    try:
        temporary.write_text(json.dumps([*records, record], sort_keys=True, indent=2))
        fence.check()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _delete_selected(con, names: set[str]) -> None:
    if "agent_events" in names:
        con.execute(
            """DELETE FROM lake.agent_events event WHERE event._import_file LIKE ?
               AND NOT EXISTS (SELECT 1 FROM lake.export_event_versions version
                 WHERE version._row_sha256=event._row_sha256)""",
            [LEGACY_PREFIX],
        )
    if "control_outbox_batches" in names:
        con.execute(
            """DELETE FROM lake.control_outbox_batches row WHERE row._import_file LIKE ?
               AND NOT EXISTS (SELECT 1 FROM lake.export_batch_receipts receipt
                 WHERE receipt.batch_id=substr(row._import_file, 8))""",
            [LEGACY_PREFIX],
        )
    if "provider_usage_snapshots" in names:
        con.execute(
            "DELETE FROM lake.provider_usage_snapshots WHERE _import_file LIKE ?",
            [LEGACY_PREFIX],
        )
    if "import_watermark" in names:
        con.execute("DELETE FROM lake.import_watermark")


def deprecate_legacy(spec: LakeSpec, *, apply: bool = False) -> dict:
    """Deprecate legacy-import rows, then recertify the resulting lake.

    The operation intentionally does not accept an allowlist or a trusted
    legacy mode: a row is retained only when existing exporter evidence proves
    it is an exporter version.
    """
    if not apply:
        return deprecation_plan(spec)
    started = monotonic()
    with (
        drained_mutation(spec.dsn()) as fence,
        lake_connection(spec, read_only=False) as con,
    ):
        plan = _selection_plan(con)
        if plan["rows"] == 0:
            return {
                "dry_run": False,
                "no_op": True,
                **plan,
                "elapsed_seconds": round(monotonic() - started, 3),
            }
        proof_snapshot, proof_digest, proof = _baseline_snapshot(spec)
        before_snapshot = con.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchone()[0]
        run_id = uuid4().hex
        intent = {
            "version": 1,
            "operation": "deprecate-legacy",
            "proof_sha256": proof_digest,
            "catalog_id": proof["catalog_id"],
            "data_root": proof["data_root"],
            "before_snapshot": before_snapshot,
            "selection_rule": SELECTION_RULE,
            "counts": plan["tables"],
        }
        intent_digest = hashlib.sha256(
            json.dumps(intent, sort_keys=True).encode()
        ).hexdigest()
        con.execute("BEGIN")
        try:
            con.execute(
                "CALL lake.set_commit_message(?, ?, extra_info => ?)",
                [MAINTENANCE_AUTHOR, run_id, intent_digest],
            )
            _delete_selected(con, _table_names(con))
            refresh_activity_daily(con, plan["affected_days"])
            fence.check()
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        after_snapshot = con.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchone()[0]
        if after_snapshot == before_snapshot:
            raise LakeError("lake_deprecation_no_snapshot")
        record = {
            **intent,
            "run_id": run_id,
            "proof_snapshot": proof_snapshot,
            "after_snapshot": after_snapshot,
            "affected_days": plan["affected_days"],
        }
        _append_deprecation_record(spec, record, fence=fence)
        _append_receipt(
            spec,
            {
                **intent,
                "tables": plan["tables"],
                "after_tables": {"deprecation": plan["tables"]},
                "after_snapshot": after_snapshot,
                "commits": [
                    [after_snapshot, MAINTENANCE_AUTHOR, run_id, intent_digest]
                ],
                "intent_sha256": intent_digest,
            },
            fence=fence,
        )
        # Keep the drain through the expensive check: no anonymous snapshot can
        # land between deletion, verification, and proof publication.
        verification = verify_partitioned(spec, fence=fence)
        proof_after = hashlib.sha256(
            (spec.data_root / "verification" / "serving-proof.json").read_bytes()
        ).hexdigest()
        from .serving_proof import advance_checkpoint, check_proof

        serving = check_proof(spec, con, proof_after)
        checkpoint = advance_checkpoint(spec, proof_after, fence=fence, con=con)
        return {
            "dry_run": False,
            "no_op": False,
            **plan,
            "before_snapshot": before_snapshot,
            "after_snapshot": after_snapshot,
            "proof_sha256": proof_after,
            "verification": verification,
            "serving_snapshot": serving["snapshot"],
            "checkpoint_snapshot": checkpoint["snapshot"] if checkpoint else None,
            "elapsed_seconds": round(monotonic() - started, 3),
        }
