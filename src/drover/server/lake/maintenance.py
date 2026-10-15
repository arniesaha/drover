"""Explicit, fenced DuckLake file and snapshot maintenance."""

from __future__ import annotations

import hashlib
import json
from time import monotonic
from uuid import uuid4

from .fence import drained_mutation
from .runtime import LakeSpec, lake_connection

EXPORT_MAINTENANCE_TABLES = (
    "agent_events",
    "activity_daily",
    "control_outbox_batches",
    "export_event_versions",
    "export_batch_receipts",
)
CLEANUP_RETENTION = "7 days"
MAINTENANCE_AUTHOR = "drover-maintenance"
MAINTENANCE_RECEIPTS = "maintenance-receipts.json"


def _file_count(con, table: str) -> int:
    row = con.execute(
        "SELECT coalesce(file_count, 0) FROM ducklake_table_info('lake') "
        "WHERE table_name = ?",
        [table],
    ).fetchone()
    return int(row[0]) if row else 0


def _snapshot_count(con) -> int:
    return int(con.execute("SELECT count(*) FROM lake.snapshots()").fetchone()[0])


def _snapshot_rows(con, after=0):
    return [
        list(row)
        for row in con.execute(
            "SELECT snapshot_id,author,commit_message,commit_extra_info "
            "FROM lake.snapshots() WHERE snapshot_id>? ORDER BY snapshot_id",
            [after],
        ).fetchall()
    ]


def _fingerprint(con, table):
    from .rebuild import accounting

    return accounting(con, "lake." + table)


def _receipt_path(spec):
    return spec.data_root / "verification" / MAINTENANCE_RECEIPTS


def _append_receipt(spec, receipt, *, fence):
    """Publish only a complete, fenced attestation; partial maintenance fails closed."""
    path = _receipt_path(spec)
    try:
        records = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(records, list):
            raise ValueError()
    except (OSError, ValueError):
        raise ValueError("maintenance receipt journal is malformed") from None
    temporary = path.with_suffix(".pending")
    try:
        temporary.write_text(json.dumps([*records, receipt], sort_keys=True))
        fence.check()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _baseline_snapshot(spec):
    """The oldest snapshot that serving may need; never let expiry cross it."""
    from .serving_proof import _checkpoint, _proof

    proof, digest = _proof(spec)
    checkpoint = _checkpoint(spec.data_root / "verification", digest, proof)
    return checkpoint["snapshot"] if checkpoint else proof["snapshot"], digest, proof


def maintain_lake(
    spec: LakeSpec, *, tables: tuple[str, ...] = EXPORT_MAINTENANCE_TABLES
):
    """Compact one catalog under the drained writer/reader fence.

    Every compaction snapshot is attested with an unchanged logical-table
    fingerprint and then checkpointed. Expiry is capped at the serving
    baseline, so neither the proof nor the current checkpoint can disappear.
    """
    selected = tuple(dict.fromkeys(tables))
    if not selected or any(
        table not in EXPORT_MAINTENANCE_TABLES for table in selected
    ):
        raise ValueError("maintenance tables must be export tables")
    started = monotonic()
    with (
        drained_mutation(spec.dsn()) as fence,
        lake_connection(spec, read_only=False) as con,
    ):
        baseline, proof_digest, proof = _baseline_snapshot(spec)
        snapshots_before = _snapshot_count(con)
        before_snapshot = max(row[0] for row in _snapshot_rows(con))
        before = {table: _fingerprint(con, table) for table in selected}
        run_id = uuid4().hex
        intent = {
            "version": 1,
            "proof_sha256": proof_digest,
            "catalog_id": proof["catalog_id"],
            "data_root": proof["data_root"],
            "before_snapshot": before_snapshot,
            "tables": before,
        }
        intent_digest = hashlib.sha256(
            json.dumps(intent, sort_keys=True).encode()
        ).hexdigest()
        report = {"tables": {}}
        for table in selected:
            before = _file_count(con, table)
            # Avoid leaving an author/message queued for a later expiry commit
            # when DuckLake has fewer than two files to merge.
            if before > 1:
                con.execute(
                    "CALL lake.set_commit_message(?, ?, extra_info => ?)",
                    [MAINTENANCE_AUTHOR, run_id, intent_digest],
                )
            merged = con.execute(
                "CALL ducklake_merge_adjacent_files('lake', ?, schema => 'main')",
                [table],
            ).fetchall()
            report["tables"][table] = {
                "files_before": before,
                "merge_outputs": len(merged),
                "files_after": _file_count(con, table),
            }
            fence.check()
        after = {table: _fingerprint(con, table) for table in selected}
        if after != intent["tables"]:
            raise ValueError("maintenance changed logical table content")
        commits = _snapshot_rows(con, before_snapshot)
        if any(
            row[1:] != [MAINTENANCE_AUTHOR, run_id, intent_digest] for row in commits
        ):
            raise ValueError("maintenance produced an unattested snapshot")
        if commits:
            receipt = {
                **intent,
                "after_snapshot": commits[-1][0],
                "commits": commits,
                "after_tables": after,
                "intent_sha256": intent_digest,
            }
            _append_receipt(spec, receipt, fence=fence)
        # DuckLake expires snapshots strictly older than this timestamp.  The
        # baseline itself is retained even when its configured 30 days elapsed.
        timestamp = con.execute(
            "SELECT snapshot_time FROM lake.snapshots() WHERE snapshot_id=?",
            [baseline],
        ).fetchone()[0]
        con.execute(
            "CALL ducklake_expire_snapshots('lake', older_than => ?)", [timestamp]
        )
        fence.check()
        con.execute(
            "CALL ducklake_cleanup_old_files('lake', "
            "older_than => now() - INTERVAL '7 days')"
        )
        fence.check()
        # The serving check validates this receipt before moving its trusted
        # start point. A crash before this step remains deliberately unservable.
        from .serving_proof import advance_checkpoint

        checkpoint = advance_checkpoint(spec, proof_digest, fence=fence)
        report.update(
            {
                "snapshots_before": snapshots_before,
                "snapshots_after": _snapshot_count(con),
                "cleanup_retention": CLEANUP_RETENTION,
                "baseline_snapshot": baseline,
                "checkpoint_snapshot": checkpoint["snapshot"] if checkpoint else None,
                "elapsed_seconds": round(monotonic() - started, 3),
            }
        )
        return report
