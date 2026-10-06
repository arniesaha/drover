"""Offline verification authorizes a baseline; only receipted exports extend it."""

import hashlib
import json
from pathlib import Path

import psycopg

from .runtime import LakeError


def catalog_identity(spec):
    with psycopg.connect(spec.dsn(), connect_timeout=2) as con:
        con.execute("SET statement_timeout = '5s'")
        rows = con.execute(
            "SELECT schema_uuid FROM public.ducklake_schema WHERE schema_name='main' AND end_snapshot IS NULL"
        ).fetchall()
    if len(rows) != 1:
        raise LakeError("lake_verification_catalog_missing")
    return str(rows[0][0])


def write_proof(spec, con, tables, *, fence, snapshot):
    """Called only after full verify, under the catalog mutation fence."""
    directory = spec.data_root / "verification"
    proof = {
        "version": 1,
        "catalog_id": catalog_identity(spec),
        "data_root": str(spec.data_root.resolve()),
        "snapshot": snapshot,
        "report_sha256": hashlib.sha256(
            (directory / "report.json").read_bytes()
        ).hexdigest(),
        "tables": tables,
    }
    payload = json.dumps(proof, sort_keys=True).encode()
    path = directory / "serving-proof.json"
    # Certify the snapshot captured BEFORE hashing, never whichever snapshot
    # happens to be current after a lost fence. Atomic publication is last.
    temporary = path.with_suffix(".pending")
    try:
        temporary.write_bytes(payload)
        fence.check()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()


def check_proof(spec, con, digest):
    from .export_worker import receipt_hash

    directory = spec.data_root / "verification"
    try:
        payload = (directory / "serving-proof.json").read_bytes()
        if len(payload) > 1024 * 1024 or hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError()
        proof = json.loads(payload)
        report = json.loads((directory / "report.json").read_bytes())
        verified = json.loads((directory / "last-verify.json").read_bytes())
        if (
            proof["version"] != 1
            or report["dry_run"]
            or proof["data_root"] != str(spec.data_root.resolve())
            or proof["catalog_id"] != catalog_identity(spec)
            or proof["tables"] != verified["tables"]
            or hashlib.sha256((directory / "report.json").read_bytes()).hexdigest()
            != proof["report_sha256"]
        ):
            raise ValueError()
        # Full rebuild verification must include all four retained tables.
        if (
            not {
                "agent_events",
                "agent_events_legacy_metadata",
                "provider_usage_snapshots",
                "control_outbox_batches",
            }
            <= proof["tables"].keys()
        ):
            raise ValueError()
        snapshots = con.execute(
            "SELECT snapshot_id,author,commit_message,commit_extra_info FROM lake.snapshots() WHERE snapshot_id>=? ORDER BY snapshot_id LIMIT 10002",
            [proof["snapshot"]],
        ).fetchall()
        if len(snapshots) > 10001:
            raise ValueError()
        if proof["snapshot"] not in {row[0] for row in snapshots}:
            raise ValueError()
        later = [row for row in snapshots if row[0] > proof["snapshot"]]
        if any(author != "drover-export" for _, author, _, _ in later):
            raise ValueError()
        receipts = _export_receipts(con, [batch for _, _, batch, _ in later])
        for _, _, batch, receipt_digest in later:
            rows = receipts.get(batch, [])
            if len(rows) != 1 or receipt_hash(rows[0]) != receipt_digest:
                raise ValueError()
        _check_referenced_files(spec, con)
    except LakeError:
        raise
    except Exception:
        raise LakeError("lake_verification_required") from None


def _export_receipts(con, batches):
    """Receipt rows for ``batches`` in one lake read, grouped by batch id.

    One lookup per export snapshot cost ~4.5 ms each and every serving child
    paid it for every export since verification: 169 snapshots after a day on
    prod were 0.8 s of a 2 s cockpit overview, growing with each export.
    """
    wanted = sorted({batch for batch in batches if batch is not None})
    if not wanted:
        return {}
    cursor = con.execute(
        "SELECT * FROM lake.export_batch_receipts"
        " WHERE batch_id IN (SELECT unnest(?::VARCHAR[]))",
        [wanted],
    )
    names = [c[0] for c in cursor.description]
    receipts = {}
    for row in cursor.fetchall():
        receipt = dict(zip(names, row))
        receipts.setdefault(receipt["batch_id"], []).append(receipt)
    return receipts


def _check_referenced_files(spec, con):
    """Bounded metadata-only coverage, including current delete files; no globs."""
    required = {
        "agent_events",
        "agent_events_legacy_metadata",
        "provider_usage_snapshots",
        "control_outbox_batches",
        "activity_daily",
    }
    optional = {"export_event_versions", "export_batch_receipts"}
    tables = {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog='lake' AND table_schema='main' LIMIT 100"
        ).fetchall()
    }
    if not required <= tables:
        raise LakeError("lake_verification_tables_missing")
    files = 0
    for table in sorted(required | (optional & tables)):
        cursor = con.execute(
            "SELECT data_file,data_file_size_bytes,delete_file,delete_file_size_bytes FROM ducklake_list_files('lake',?)",
            [table],
        )
        while row := cursor.fetchone():
            for name, size in ((row[0], row[1]), (row[2], row[3])):
                if name is None:
                    continue
                files += 1
                if files > 10000:
                    raise LakeError("lake_verification_file_limit")
                path = Path(name)
                if not path.resolve().is_relative_to(spec.data_root.resolve()):
                    raise LakeError("lake_verification_file_outside_root")
                try:
                    if not path.is_file() or path.stat().st_size != size:
                        raise LakeError("lake_verification_file_missing_or_changed")
                except OSError:
                    raise LakeError(
                        "lake_verification_file_missing_or_changed"
                    ) from None
