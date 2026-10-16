"""Offline verification authorizes a baseline; only receipted exports extend it."""

import hashlib
import json
from pathlib import Path

import psycopg

from .runtime import LakeError

# Written by the hub's exporter; see advance_checkpoint. The config pins the
# proof, never this file, so advancing it needs no config edit or restart.
CHECKPOINT_NAME = "serving-checkpoint.json"
# Bounds one serving child's chain check: the checkpoint snapshot plus 10,000
# exports. The exporter checkpoints far more often than that.
MAX_SNAPSHOTS_SINCE_CHECK = 10001
# Every serving child stats every referenced file (~30 us each on prod), and
# nothing compacts the lake yet: each export adds ~4 files. 10,000 left about
# 2,200 exports after the prod switch (hours at 2.5-11 exports/min); 50,000
# costs ~1.5 s per child at the limit, inside the 5 s deadline. Raising the
# bound changes no check. Compaction is what keeps the count down.
REFERENCED_FILE_LIMIT = 50_000


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


def proof_pin(spec) -> str:
    """Return the SHA-256 that must be pinned for the published proof."""
    return hashlib.sha256(
        (spec.data_root / "verification" / "serving-proof.json").read_bytes()
    ).hexdigest()


def proof_pin_hint(digest: str) -> str:
    """Return the exact configuration assignment for a proof digest."""
    return f'[analytics] verification_sha256 = "{digest}"'


def check_proof(spec, con, digest):
    from .export_worker import receipt_hash

    directory = spec.data_root / "verification"
    try:
        proof, actual_digest = _proof(spec)
        if actual_digest != digest:
            raise LakeError(
                "lake_verification_required",
                "analytics.verification_sha256 mismatch: "
                f"expected {digest}; actual {actual_digest}",
            )
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
        # Exports up to a checkpoint were checked exactly like the ones below
        # when it was written; only those after it are checked again here.
        checkpoint = _checkpoint(directory, digest, proof)
        start = checkpoint["snapshot"] if checkpoint else proof["snapshot"]
        snapshots = con.execute(
            "SELECT snapshot_id,author,commit_message,commit_extra_info FROM lake.snapshots() WHERE snapshot_id>=? ORDER BY snapshot_id LIMIT 10002",
            [start],
        ).fetchall()
        if len(snapshots) > MAX_SNAPSHOTS_SINCE_CHECK:
            raise ValueError()
        if not snapshots or snapshots[0][0] != start:
            raise ValueError()
        # A checkpointed snapshot that is gone or no longer the export it was
        # (catalog rollback, restore, rewrite) is a changed lake: fail closed.
        if checkpoint and list(snapshots[0][1:]) != checkpoint["commit"]:
            raise ValueError()
        later = snapshots[1:]
        exports = [row for row in later if row[1] == "drover-export"]
        maintenance = [row for row in later if row[1] == "drover-maintenance"]
        if len(exports) + len(maintenance) != len(later):
            raise ValueError()
        _check_export_receipts(con, exports)
        # A checkpoint is trusted only after its own maintenance receipt still
        # validates; otherwise deleting/tampering that receipt would turn a
        # formerly accepted maintenance boundary into a blind spot.
        checked_maintenance = maintenance
        if checkpoint and snapshots[0][1] == "drover-maintenance":
            checked_maintenance = [snapshots[0], *maintenance]
        _check_maintenance_receipts(directory, digest, proof, checked_maintenance)
        _check_referenced_files(spec, con)
        head = snapshots[-1]
        return {
            "version": 1,
            "proof_sha256": digest,
            "catalog_id": proof["catalog_id"],
            "data_root": proof["data_root"],
            "from_snapshot": start,
            "snapshot": head[0],
            "commit": list(head[1:]),
            "exports": (checkpoint["exports"] if checkpoint else 0) + len(exports),
            "chain_sha256": _chain(
                checkpoint["chain_sha256"] if checkpoint else digest, later
            ),
        }
    except LakeError:
        raise
    except Exception:
        raise LakeError("lake_verification_required") from None


def _chain(previous, rows):
    """Per-export hash chain from the proof: the same value however the
    exports were split across checkpoints, so an audit can replay it."""
    for row in rows:
        previous = hashlib.sha256(
            (previous + json.dumps(list(row))).encode()
        ).hexdigest()
    return previous


def _checkpoint(directory, digest, proof):
    """The checkpoint bound to this proof, or None to check from the proof.

    A missing, malformed or foreign checkpoint (an older proof, another lake)
    only means the whole chain since the proof is checked, as before
    checkpoints existed; it never widens what is accepted.
    """
    try:
        payload = (directory / CHECKPOINT_NAME).read_bytes()
        if len(payload) > 64 * 1024:
            return None
        record = json.loads(payload)
    except (OSError, ValueError):
        return None
    if (
        not isinstance(record, dict)
        or record.get("version") != 1
        or record.get("proof_sha256") != digest
        or record.get("catalog_id") != proof["catalog_id"]
        or record.get("data_root") != proof["data_root"]
        or type(record.get("snapshot")) is not int
        or record["snapshot"] <= proof["snapshot"]
        or not isinstance(record.get("commit"), list)
        or len(record["commit"]) != 3
        or record["commit"][0] not in {"drover-export", "drover-maintenance"}
        or type(record.get("exports")) is not int
        or record["exports"] < 0
        or not isinstance(record.get("chain_sha256"), str)
        or len(record["chain_sha256"]) != 64
    ):
        return None
    return record


def _proof(spec):
    """Load the config-pinned proof before accepting any local attestation."""
    payload = (spec.data_root / "verification" / "serving-proof.json").read_bytes()
    if len(payload) > 1024 * 1024:
        raise ValueError()
    return json.loads(payload), hashlib.sha256(payload).hexdigest()


def _check_maintenance_receipts(directory, digest, proof, snapshots):
    """Accept only a journal entry committed by the fenced maintenance run.

    The catalog stores an intent hash in every maintenance snapshot. The local
    entry binds that intent to the proof, exact before/after snapshots and
    equal before/after logical table fingerprints. Thus a foreign snapshot or
    a modified receipt cannot become a checkpoint boundary.
    """
    if not snapshots:
        return
    from .maintenance import MAINTENANCE_RECEIPTS

    try:
        records = json.loads((directory / MAINTENANCE_RECEIPTS).read_text())
        if not isinstance(records, list):
            raise ValueError()
    except (OSError, ValueError):
        raise ValueError() from None
    remaining = {tuple(row) for row in snapshots}
    for record in records:
        try:
            intent = {
                key: record[key]
                for key in (
                    "version",
                    "proof_sha256",
                    "catalog_id",
                    "data_root",
                    "before_snapshot",
                    "tables",
                )
            }
            expected = hashlib.sha256(
                json.dumps(intent, sort_keys=True).encode()
            ).hexdigest()
            commits = {tuple(row) for row in record["commits"]}
            # Old receipts naturally precede a later checkpoint. They are
            # irrelevant here; a partially overlapping receipt is not.
            if not commits & remaining:
                continue
            if (
                record["version"] != 1
                or record["proof_sha256"] != digest
                or record["catalog_id"] != proof["catalog_id"]
                or record["data_root"] != proof["data_root"]
                or record["intent_sha256"] != expected
                or record["tables"] != record["after_tables"]
                or not commits <= remaining
                or not commits
                or min(row[0] for row in commits) <= record["before_snapshot"]
                or record["after_snapshot"] != max(row[0] for row in commits)
                or any(
                    row[1] != "drover-maintenance" or row[3] != expected
                    for row in commits
                )
            ):
                raise ValueError()
            remaining -= commits
        except (KeyError, TypeError, ValueError):
            raise ValueError() from None
    if remaining:
        raise ValueError()


def advance_checkpoint(spec, digest, *, fence, con=None):
    """Run the serving check at the lake head and record it as the checkpoint.

    Only the lake's single writer calls this, holding its mutation fence, so
    nothing it did not check can be committed in between. The check normally
    runs in an admitted serving child. Fenced maintenance supplies its
    already-drained writer connection because its drain intentionally blocks a
    child reader; a failure leaves the old checkpoint.
    """
    if con is None:
        from .query_process import query

        record = query(
            spec,
            "SELECT 1",
            serving={"verification_sha256": digest, "checkpoint": True},
        )["checkpoint"]
    else:
        record = check_proof(spec, con, digest)
    if record["snapshot"] == record["from_snapshot"]:
        return None
    path = spec.data_root / "verification" / CHECKPOINT_NAME
    temporary = path.with_suffix(".pending")
    try:
        temporary.write_text(json.dumps(record, sort_keys=True))
        fence.check()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return record


def _export_receipts(con, batches):
    """Receipt rows for ``batches`` in one lake read, grouped by batch id.

    One lookup per export snapshot cost ~4.5 ms each and every serving child
    paid it for every export since verification: 169 snapshots ~1.5 h after
    the prod switch were 0.8 s of a 2 s cockpit overview.
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


def _check_export_receipts(con, exports):
    """Check snapshot receipt commitments using the serving proof's rule."""
    from .export_worker import receipt_hash

    receipts = _export_receipts(con, [batch for _, _, batch, _ in exports])
    for _, _, batch, receipt_digest in exports:
        rows = receipts.get(batch, [])
        if len(rows) != 1 or receipt_hash(rows[0]) != receipt_digest:
            raise ValueError()


def validate_export_receipts(con):
    """Validate every exporter receipt against its committed export snapshot.

    Full offline verification starts from frozen rows and may deliberately
    recertify a provisioning snapshot that a serving proof rejects.  It still
    uses the exact receipt commitment rule serving uses for every export.
    """
    from .export_worker import receipt_hash

    missing = con.execute("""SELECT 1 FROM lake.export_batch_receipts receipt
            LEFT JOIN lake.snapshots() snapshot
              ON snapshot.author='drover-export'
             AND snapshot.commit_message=receipt.batch_id
            WHERE snapshot.snapshot_id IS NULL LIMIT 1""").fetchone()
    orphan = con.execute("""SELECT 1 FROM lake.snapshots() snapshot
            LEFT JOIN lake.export_batch_receipts receipt
              ON receipt.batch_id=snapshot.commit_message
            WHERE snapshot.author='drover-export' AND receipt.batch_id IS NULL
            LIMIT 1""").fetchone()
    if missing or orphan:
        raise ValueError()
    cursor = con.execute("""SELECT snapshot.commit_extra_info,receipt.*
            FROM lake.snapshots() snapshot
            JOIN lake.export_batch_receipts receipt
              ON receipt.batch_id=snapshot.commit_message
            WHERE snapshot.author='drover-export' ORDER BY snapshot.snapshot_id""")
    names = [column[0] for column in cursor.description][1:]
    while rows := cursor.fetchmany(1024):
        for row in rows:
            if receipt_hash(dict(zip(names, row[1:], strict=True))) != row[0]:
                raise ValueError()


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
    root = spec.data_root.resolve()
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
                if files > REFERENCED_FILE_LIMIT:
                    raise LakeError("lake_verification_file_limit")
                path = Path(name)
                if not path.resolve().is_relative_to(root):
                    raise LakeError("lake_verification_file_outside_root")
                try:
                    if not path.is_file() or path.stat().st_size != size:
                        raise LakeError("lake_verification_file_missing_or_changed")
                except OSError:
                    raise LakeError(
                        "lake_verification_file_missing_or_changed"
                    ) from None
