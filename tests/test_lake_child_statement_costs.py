"""Per-row lake statements in every serving child (cockpit overview latency).

Plain DuckDB with a ``lake`` schema stands in for the attached DuckLake
catalog, so these run without the pinned lake extensions.
"""

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import duckdb
import pytest

from drover.server.db import _POSTGRES_ANALYTICS_SNAPSHOT_TABLES
from drover.server.lake import serving_proof
from drover.server.lake.export_worker import RECEIPT_SCHEMA, receipt_hash
from drover.server.lake.read_models import _load_native_usage
from drover.server.lake.runtime import LakeError

TABLES = {
    name: {"rows": 1}
    for name in (
        "agent_events",
        "agent_events_legacy_metadata",
        "provider_usage_snapshots",
        "control_outbox_batches",
    )
}


class CountingConnection:
    def __init__(self, con):
        self.con = con
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return self.con.execute(sql, params)


def _receipt(batch):
    return {
        "batch_id": batch,
        "catalog_id": "catalog",
        "contract_version": 1,
        "input_sha256": hashlib.sha256(batch.encode()).hexdigest(),
        "raw_rows": 3,
        "canonical_rows": 3,
        "raw_sha256": "a" * 64,
        "canonical_sha256": "b" * 64,
    }


@pytest.fixture
def proven_lake(tmp_path, monkeypatch):
    """A verified baseline at snapshot 1 followed by ``exports`` receipted exports."""
    monkeypatch.setattr(serving_proof, "catalog_identity", lambda spec: "catalog")
    monkeypatch.setattr(serving_proof, "_check_referenced_files", lambda *_: None)
    root = tmp_path / "lake"
    directory = root / "verification"
    directory.mkdir(parents=True)
    report = json.dumps({"dry_run": False}).encode()
    (directory / "report.json").write_bytes(report)
    (directory / "last-verify.json").write_text(json.dumps({"tables": TABLES}))
    proof = json.dumps(
        {
            "version": 1,
            "catalog_id": "catalog",
            "data_root": str(root.resolve()),
            "snapshot": 1,
            "report_sha256": hashlib.sha256(report).hexdigest(),
            "tables": TABLES,
        },
        sort_keys=True,
    ).encode()
    (directory / "serving-proof.json").write_bytes(proof)
    con = duckdb.connect()
    con.execute("CREATE SCHEMA lake")
    columns = ",".join(f"{name} {kind}" for name, kind in RECEIPT_SCHEMA.items())
    con.execute(f"CREATE TABLE lake.export_batch_receipts ({columns})")
    con.execute(
        "CREATE TABLE lake.snapshot_log(snapshot_id BIGINT, author VARCHAR,"
        " commit_message VARCHAR, commit_extra_info VARCHAR)"
    )
    con.execute(
        "CREATE MACRO lake.snapshots() AS TABLE SELECT * FROM lake.snapshot_log"
    )
    con.execute("INSERT INTO lake.snapshot_log VALUES (1, 'verify', NULL, NULL)")

    def export(snapshot, batch, *, digest=None, author="drover-export"):
        receipt = _receipt(batch)
        con.execute(
            "INSERT INTO lake.export_batch_receipts VALUES (?,?,?,?,?,?,?,?)",
            list(receipt.values()),
        )
        con.execute(
            "INSERT INTO lake.snapshot_log VALUES (?,?,?,?)",
            [snapshot, author, batch, digest or receipt_hash(receipt)],
        )

    for index in range(60):
        export(index + 2, f"batch-{index:03d}")
    spec = SimpleNamespace(data_root=root)
    yield spec, con, hashlib.sha256(proof).hexdigest(), export
    con.close()


def test_export_receipts_are_checked_in_one_lake_read(proven_lake):
    # One lake read per export snapshot was 0.8 s of a 2 s prod cockpit
    # overview after a day of exports (169 snapshots), and grew with each.
    spec, con, digest, _ = proven_lake
    counting = CountingConnection(con)

    serving_proof.check_proof(spec, counting, digest)

    receipt_reads = [s for s in counting.statements if "export_batch_receipts" in s]
    assert len(receipt_reads) == 1
    assert len(counting.statements) == 2


def test_tampered_receipt_digest_still_requires_verification(proven_lake):
    spec, con, digest, export = proven_lake
    export(100, "batch-tampered", digest="0" * 64)

    with pytest.raises(LakeError, match="lake_verification_required"):
        serving_proof.check_proof(spec, con, digest)


def test_export_without_receipt_still_requires_verification(proven_lake):
    spec, con, digest, _ = proven_lake
    con.execute("INSERT INTO lake.snapshot_log VALUES (100,'drover-export','lost','x')")

    with pytest.raises(LakeError, match="lake_verification_required"):
        serving_proof.check_proof(spec, con, digest)


def test_duplicate_receipt_rows_still_require_verification(proven_lake):
    spec, con, digest, _ = proven_lake
    con.execute(
        "INSERT INTO lake.export_batch_receipts"
        " SELECT * FROM lake.export_batch_receipts WHERE batch_id='batch-007'"
    )

    with pytest.raises(LakeError, match="lake_verification_required"):
        serving_proof.check_proof(spec, con, digest)


def test_non_export_snapshot_after_proof_still_requires_verification(proven_lake):
    spec, con, digest, export = proven_lake
    export(100, "batch-manual", author="operator")

    with pytest.raises(LakeError, match="lake_verification_required"):
        serving_proof.check_proof(spec, con, digest)


def _session_usage(con):
    columns = _POSTGRES_ANALYTICS_SNAPSHOT_TABLES["session_usage"]
    definitions = ",".join(f'"{name}" {kind}' for name, kind in columns)
    con.execute(f"CREATE TEMP TABLE session_usage ({definitions})")
    con.execute(
        "INSERT INTO session_usage(session_id,input_tokens,source)"
        " VALUES ('pg-owned', 7, 'control_events')"
    )


def _native(session_id, tokens):
    return {
        "session_id": session_id,
        "input_tokens": tokens,
        "output_tokens": None,
        "cache_read_tokens": 2**40,
        "cache_write_tokens": 0,
        "reasoning_tokens": None,
        "turn_count": 3,
        "source_event_count": 4,
    }


def test_native_usage_loads_in_one_statement_without_overriding_pg():
    # One INSERT per certified native session cost ~0.45 s at prod's ~830.
    con = duckdb.connect()
    _session_usage(con)
    usage = [_native("pg-owned", 99)] + [_native(f"s-{i}", i) for i in range(500)]
    counting = CountingConnection(con)

    _load_native_usage(counting, usage, "2026-10-06T21:00:00+00:00")

    assert len(counting.statements) == 1
    assert con.execute(
        "SELECT input_tokens, source FROM session_usage WHERE session_id='pg-owned'"
    ).fetchall() == [(7, "control_events")]
    rows = con.execute(
        """SELECT session_id,input_tokens,output_tokens,cache_read_tokens,
        cache_write_tokens,reasoning_tokens,turn_count,exact,source,
        source_event_count,observed_at FROM session_usage
        WHERE source='native_agent_events' ORDER BY input_tokens"""
    ).fetchall()
    assert len(rows) == 500
    assert rows[3][:10] == (
        "s-3",
        3,
        None,
        2**40,
        0,
        None,
        3,
        True,
        "native_agent_events",
        4,
    )
    assert rows[3][10] == datetime(2026, 10, 6, 21, tzinfo=timezone.utc)


def test_native_usage_load_is_a_no_op_without_rows():
    con = duckdb.connect()
    _session_usage(con)
    counting = CountingConnection(con)

    _load_native_usage(counting, [], "2026-10-06T21:00:00+00:00")

    assert counting.statements == []
