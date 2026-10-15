"""Find and explicitly repair control acknowledgements absent from a lake."""

from __future__ import annotations

import json

from drover.server.control_outbox import canonical_payload, payload_sha256
from drover.server.db import control_plane_connection

from .export_worker import export, receipt_hash, validate_input
from .fence import drained_mutation
from .runtime import LakeError, LakeSpec, lake_connection
from .serving_proof import catalog_identity


def missing_acknowledged(control_path, spec: LakeSpec):
    """Return immutable frozen inputs acknowledged for this catalog but absent in it."""
    catalog_id = catalog_identity(spec)
    with control_plane_connection(control_path, timeout=2) as control:
        rows = control.execute(
            """SELECT x.batch_id,x.input_json,x.input_sha256,x.receipt_sha256
            FROM lake_export_batches x JOIN control_outbox_batches b USING (batch_id)
            WHERE x.catalog_id=? AND x.acknowledged_at IS NOT NULL
              AND b.acknowledged_at IS NOT NULL ORDER BY b.acknowledged_at,x.batch_id""",
            [catalog_id],
        ).fetchall()
    batch_ids = [row[0] for row in rows]
    if not batch_ids:
        return []
    with lake_connection(spec) as lake:
        found = {
            row[0]
            for row in lake.execute(
                "SELECT batch_id FROM lake.export_batch_receipts "
                "WHERE batch_id IN (SELECT unnest(?::VARCHAR[]))",
                [batch_ids],
            ).fetchall()
        }
    missing = []
    for batch_id, input_json, input_sha256, stored_receipt in rows:
        if batch_id in found or payload_sha256(input_json) != input_sha256:
            continue
        document = json.loads(input_json)
        validate_input(document)
        if (
            document.get("catalog_id") != catalog_id
            or document.get("batch_id") != batch_id
        ):
            raise LakeError("lake_reconcile_input_mismatch")
        missing.append(
            {
                "batch_id": batch_id,
                "input_sha256": input_sha256,
                "receipt_sha256": stored_receipt,
                "document": document,
            }
        )
    return missing


def reconcile_acknowledged(control_path, spec: LakeSpec, *, apply=False):
    """Dry-run by default; republish exact frozen documents only when requested.

    ``export`` merges candidates with current rows using its deterministic
    version ordering. Consequently an old repaired batch can append its
    immutable evidence but cannot replace a newer canonical winner.
    """
    missing = missing_acknowledged(control_path, spec)
    report = {
        "dry_run": not apply,
        "missing": [r["batch_id"] for r in missing],
        "repaired": [],
    }
    if not apply or not missing:
        return report
    with drained_mutation(spec.dsn()) as fence:
        for item in missing:
            fence.check()
            result = export(spec, item["document"])
            fence.check()
            digest = receipt_hash(result["receipt"])
            if item["receipt_sha256"] not in (None, digest):
                raise LakeError("lake_reconcile_receipt_mismatch")
            with control_plane_connection(control_path, timeout=2) as control:
                control.execute(
                    "UPDATE lake_export_batches SET receipt_sha256=? "
                    "WHERE batch_id=? AND acknowledged_at IS NOT NULL",
                    [digest, item["batch_id"]],
                )
            report["repaired"].append(
                {"batch_id": item["batch_id"], "replayed": result["replayed"]}
            )
    return report
