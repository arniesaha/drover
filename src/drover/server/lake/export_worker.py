"""One bounded, isolated lake transaction for an immutable outbox input."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pyarrow as pa

from drover.server.control_outbox import _batch_id, canonical_payload, payload_sha256

from .rebuild import EVENT_SCHEMA, OUTBOX_SCHEMA, row_hash_expression
from .rebuild_worker import LINEAGE, POLICY_SCHEMA
from .runtime import LakeError, LakeSpec, attach_lake, literal

RECEIPT_SCHEMA = {
    "batch_id": "VARCHAR",
    "catalog_id": "VARCHAR",
    "contract_version": "INTEGER",
    "input_sha256": "VARCHAR",
    "raw_rows": "BIGINT",
    "canonical_rows": "BIGINT",
    "raw_sha256": "VARCHAR",
    "canonical_sha256": "VARCHAR",
}


def receipt_hash(receipt):
    return payload_sha256(canonical_payload(receipt))


def validate_input(document):
    if document.get("contract_version") != 1:
        raise LakeError("lake_export_contract_mismatch")
    rows = document["raw_rows"]
    ids = document["event_ids"]
    if not rows or len(rows) > 1000 or len(set(ids)) != len(ids):
        raise LakeError("lake_export_membership_mismatch")
    if [r["event_id"] for r in rows] != ids or _batch_id(ids) != document["batch_id"]:
        raise LakeError("lake_export_membership_mismatch")
    if [r["outbox_ordinal"] for r in rows] != list(range(len(rows))):
        raise LakeError("lake_export_membership_mismatch")
    for row in rows:
        if (
            not isinstance(row["payload_json"], str)
            or payload_sha256(row["payload_json"]) != row["payload_sha256"]
        ):
            raise LakeError("lake_export_payload_hash_mismatch")
    if len(document["events"]) != len(rows):
        raise LakeError("lake_export_projection_mismatch")
    for raw, event in zip(rows, document["events"], strict=True):
        if (
            event.get("id") != raw["event_id"]
            or event.get("session_id") != raw["session_id"]
            or not event.get("dedup_key")
        ):
            raise LakeError("lake_export_projection_mismatch")


def _stage(con, name, rows, schema):
    # JSON arrives with UTC strings; explicit casts avoid Arrow's NULL inference
    # or timestamp drift. All optional historical columns are declared.
    incoming = pa.Table.from_pylist([{k: row.get(k) for k in schema} for row in rows])
    con.register("incoming", incoming)
    try:
        projection = ",".join(f'CAST("{k}" AS {t}) AS "{k}"' for k, t in schema.items())
        con.execute(f"CREATE TEMP TABLE {name} AS SELECT {projection} FROM incoming")
    finally:
        con.unregister("incoming")


def _multiset(con, relation):
    from .rebuild import hash_multiset

    return hash_multiset(con, relation)


def _event_order(alias):
    return f"""({alias}.repo_owner IS NOT NULL AND {alias}.repo_name IS NOT NULL) DESC,
        TRY_CAST({alias}.timestamp AS TIMESTAMPTZ) DESC NULLS LAST,
        {alias}.id DESC NULLS LAST, {alias}._winner_hash DESC,
        {alias}._import_file, {alias}._import_row"""


def prepare(con, document):
    validate_input(document)
    batch_id = document["batch_id"]
    events = []
    for i, event in enumerate(document["events"]):
        events.append(
            {
                **event,
                "dedup_key_source": "outbox",
                "_source_dedup_key": event["dedup_key"],
                "_import_file": "outbox:" + batch_id,
                "_import_row": i,
            }
        )
    _stage(
        con,
        "candidate_events",
        events,
        POLICY_SCHEMA | {k: v for k, v in LINEAGE.items() if k != "_row_sha256"},
    )
    con.execute(
        """CREATE TEMP TABLE winners AS SELECT * EXCLUDE (rank) FROM (
        SELECT *, row_number() OVER (PARTITION BY dedup_key ORDER BY """
        + _event_order("e")
        + """ ) AS rank FROM (SELECT *, """
        + row_hash_expression(EVENT_SCHEMA)
        + " AS _winner_hash FROM candidate_events) e) WHERE rank=1"
    )
    con.execute(
        "CREATE TEMP TABLE export_events AS SELECT * EXCLUDE (_winner_hash), "
        + row_hash_expression(POLICY_SCHEMA)
        + " AS _row_sha256 FROM winners"
    )
    raw = [
        {**r, "_import_file": "outbox:" + batch_id, "_import_row": r["outbox_ordinal"]}
        for r in document["raw_rows"]
    ]
    _stage(
        con,
        "raw_candidate",
        raw,
        OUTBOX_SCHEMA | {k: v for k, v in LINEAGE.items() if k != "_row_sha256"},
    )
    con.execute(
        "CREATE TEMP TABLE export_raw AS SELECT *, "
        + row_hash_expression(OUTBOX_SCHEMA)
        + " AS _row_sha256 FROM raw_candidate"
    )
    return {
        "batch_id": batch_id,
        "catalog_id": document["catalog_id"],
        "contract_version": 1,
        "input_sha256": payload_sha256(canonical_payload(document)),
        "raw_rows": len(raw),
        "canonical_rows": con.execute("SELECT count(*) FROM export_events").fetchone()[
            0
        ],
        "raw_sha256": _multiset(con, "export_raw"),
        "canonical_sha256": _multiset(con, "export_events"),
    }


def _merge_events(con):
    # Target comparison uses the same pre-lineage payload hash as rebuild's
    # winner ranking. Attribution/timestamp/ID ties have a deterministic suffix.
    # Use a union of only the affected target keys to choose one source winner;
    # no full-history analytical projection or NULL-key merge is involved.
    con.execute(
        """CREATE TEMP TABLE affected AS
        SELECT t.*, """
        + row_hash_expression(EVENT_SCHEMA)
        + " AS _winner_hash FROM lake.agent_events t WHERE dedup_key IN (SELECT dedup_key FROM export_events)"
    )
    columns = list(POLICY_SCHEMA | LINEAGE)
    con.execute(
        """CREATE TEMP TABLE merged_winners AS SELECT """
        + ",".join(f'"{c}"' for c in columns)
        + " FROM (SELECT *, row_number() OVER (PARTITION BY dedup_key ORDER BY "
        + _event_order("e")
        + ") AS rank FROM (SELECT * FROM affected UNION ALL BY NAME SELECT *, "
        + row_hash_expression(EVENT_SCHEMA)
        + " AS _winner_hash FROM export_events) e) WHERE rank=1"
    )
    assignments = ",".join(f'"{c}"=s."{c}"' for c in columns)
    con.execute(
        f"""MERGE INTO lake.agent_events t USING merged_winners s ON t.dedup_key=s.dedup_key
        WHEN MATCHED AND t._row_sha256 IS DISTINCT FROM s._row_sha256 THEN UPDATE SET {assignments}
        WHEN NOT MATCHED THEN INSERT BY NAME"""
    )


def export(spec, document, *, before_commit=None):
    spill = spec.data_root / "spill"
    spill.mkdir(exist_ok=True)
    with duckdb.connect(
        config={
            "memory_limit": "1GB",
            "threads": 1,
            "temp_directory": str(spill),
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
        }
    ) as con:
        con.execute("SET TimeZone='UTC'")
        expected = prepare(con, document)
        attach_lake(con, spec, read_only=False)
        con.execute("BEGIN")
        try:
            cur = con.execute(
                "SELECT * FROM lake.export_batch_receipts WHERE batch_id=?",
                [document["batch_id"]],
            )
            columns = [d[0] for d in cur.description]
            existing = [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]
            if existing:
                if existing != [expected]:
                    raise LakeError("lake_export_receipt_mismatch")
                # Receipt and exact raw membership/hash must agree. A filesystem
                # orphan is never evidence of a successful lake publication.
                con.execute(
                    "CREATE TEMP VIEW committed_raw AS SELECT * FROM lake.control_outbox_batches WHERE _import_file="
                    + literal("outbox:" + document["batch_id"])
                )
                from .rebuild import hash_multiset

                if (
                    con.execute("SELECT count(*) FROM committed_raw").fetchone()[0]
                    != expected["raw_rows"]
                    or hash_multiset(
                        con,
                        "committed_raw",
                        expression=row_hash_expression(OUTBOX_SCHEMA),
                    )
                    != expected["raw_sha256"]
                ):
                    raise LakeError("lake_export_receipt_content_mismatch")
                con.execute(
                    "CREATE TEMP VIEW committed_versions AS SELECT * FROM lake.export_event_versions WHERE _import_file="
                    + literal("outbox:" + document["batch_id"])
                )
                if (
                    con.execute("SELECT count(*) FROM committed_versions").fetchone()[0]
                    != expected["canonical_rows"]
                    or hash_multiset(
                        con,
                        "committed_versions",
                        expression=row_hash_expression(POLICY_SCHEMA),
                    )
                    != expected["canonical_sha256"]
                ):
                    raise LakeError("lake_export_receipt_content_mismatch")
                con.execute("COMMIT")
                return {"receipt": expected, "replayed": True}
            _merge_events(con)
            con.execute(
                "INSERT INTO lake.export_event_versions BY NAME SELECT * FROM export_events"
            )
            con.execute(
                "INSERT INTO lake.control_outbox_batches BY NAME SELECT * FROM export_raw"
            )
            _stage(con, "new_receipt", [expected], RECEIPT_SCHEMA)
            con.execute(
                "INSERT INTO lake.export_batch_receipts BY NAME SELECT * FROM new_receipt"
            )
            if before_commit:
                before_commit()
            con.execute("COMMIT")
            return {"receipt": expected, "replayed": False}
        except BaseException:
            # A failed catalog COMMIT may already have ended the DuckDB
            # transaction. Preserve the original failure rather than masking it.
            try:
                con.execute("ROLLBACK")
            except duckdb.TransactionException:
                pass
            raise


if __name__ == "__main__":
    request = json.loads(Path(sys.argv[1]).read_text())
    try:
        spec = LakeSpec(
            **{
                **request["spec"],
                "data_root": Path(request["spec"]["data_root"]),
                "extension_dir": Path(request["spec"]["extension_dir"]),
            }
        )
        result = export(spec, request["document"])
    except LakeError as exc:
        result = {"error": exc.code}
    except Exception:
        result = {"error": "lake_export_transaction_failed"}
    Path(request["reply"]).write_text(json.dumps(result))
