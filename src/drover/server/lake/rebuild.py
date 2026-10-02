"""Offline frozen-tar rebuild with a single declared schema and dedupe lineage."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import time
from contextlib import nullcontext
from pathlib import Path, PurePosixPath

import duckdb
import psycopg

from .fence import drained_mutation
from .runtime import (
    LakeError,
    LakeSpec,
    attach_lake,
    configure_catalog,
    create_table,
    lake_connection,
    literal,
    sha256_file,
    verify_runtime,
)

# Schema is intentionally explicit, including VARCHAR event timestamps. Binding
# all files once prevents mixed-era timestamps changing representation by batch.
EVENT_SCHEMA = (
    dict.fromkeys(
        [
            "id",
            "session_id",
            "timestamp",
            "event_type",
            "role",
            "content",
            "repo_owner",
            "repo_name",
            "branch",
            "task_id",
            "principal_id",
            "dedup_key",
            "raw_data",
            "date",
            "agent_id",
            "git_repo",
            "git_branch",
            "cwd",
            "slug",
            "entrypoint",
            "prompt_id",
            "request_id",
            "sub_agent_id",
            "permission_mode",
            "stop_reason",
            "parent_uuid",
            "message_uuid",
        ],
        "VARCHAR",
    )
    | {"is_api_error": "BOOLEAN", "is_sidechain": "BOOLEAN"}
    | dict.fromkeys(
        [
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ],
        "BIGINT",
    )
)
PROVIDER_SCHEMA = (
    dict.fromkeys(
        [
            "snapshot_id",
            "dedup_key",
            "provider",
            "account_label",
            "account_identity",
            "plan_label",
            "host_id",
            "status",
            "source",
            "error_category",
            "window_kind",
            "unit",
        ],
        "VARCHAR",
    )
    | dict.fromkeys(["observed_at", "starts_at", "resets_at"], "TIMESTAMPTZ")
    | dict.fromkeys(
        [
            "used_percent",
            "limit_value",
            "remaining_value",
        ],
        "DOUBLE",
    )
    | {"window_minutes": "BIGINT"}
)
OUTBOX_SCHEMA = dict.fromkeys(
    [
        "event_id",
        "session_id",
        "event_type",
        "normalized_type",
        "normalized_source",
        "content_preview",
        "dedup_key",
        "payload_json",
        "payload_sha256",
    ],
    "VARCHAR",
) | {"outbox_ordinal": "BIGINT", "seq": "BIGINT", "created_at": "TIMESTAMPTZ"}
SCHEMAS = {
    "agent_events": EVENT_SCHEMA,
    "provider_usage_snapshots": PROVIDER_SCHEMA,
    "control_outbox_batches": OUTBOX_SCHEMA,
}


def _selected(member: tarfile.TarInfo) -> str | None:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise LakeError("rebuild_unsafe_tar_path")
    if any(part.startswith("._") for part in path.parts):
        return None
    table = next((t for t in SCHEMAS if t in path.parts), None)
    if table and path.suffix == ".parquet":
        if not member.isfile():
            raise LakeError("rebuild_tar_requires_regular_files")
        return table
    return None


def extract_frozen(source: Path, destination: Path) -> dict[str, list[dict]]:
    """Enumerate before extraction, reject aliases, copy only the three relations."""
    inventory = {t: [] for t in SCHEMAS}
    total = 0
    seen = set()
    with tarfile.open(source) as archive:
        for member in archive:
            table = _selected(member)
            if table:
                if member.name in seen:
                    raise LakeError("rebuild_duplicate_tar_member")
                seen.add(member.name)
                total += member.size
                inventory[table].append({"path": member.name, "bytes": member.size})
    # Raw input plus materialized staging, spills, lake output and manifests.
    if shutil.disk_usage(destination.parent).free < max(total * 12, 1024**3):
        raise LakeError("rebuild_insufficient_free_space")
    if any(not files for files in inventory.values()):
        raise LakeError("rebuild_missing_relation")
    by_path = {f["path"]: f for files in inventory.values() for f in files}
    with tarfile.open(source) as archive:
        for member in archive:
            if _selected(member):
                path = destination / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with archive.extractfile(member) as stream, path.open("xb") as output:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                        output.write(block)
                by_path[member.name]["sha256"] = digest.hexdigest()
    return inventory


def row_hash_expression(schema: dict[str, str]) -> str:
    # Fixed column order, explicit UTC timezone on connections, JSON escaping and
    # null markers define the normalized-row representation (version 1).
    fields = ", ".join(f'"{name}" := "{name}"' for name in schema)
    return f"sha256(to_json(struct_pack({fields})))"


def hash_multiset(con, relation: str, *, expression: str = "_row_sha256") -> str:
    digest = hashlib.sha256()
    cursor = con.execute(
        f"SELECT {expression} AS digest FROM {relation} ORDER BY digest"
    )
    while rows := cursor.fetchmany(1000):
        for (row_hash,) in rows:
            digest.update(row_hash.encode("ascii") + b"\n")
    return digest.hexdigest()


def stage_relation(con, table: str, inventory: list[dict], extracted: Path):
    schema = SCHEMAS[table]
    files = [str(extracted / item["path"]) for item in inventory]
    con.execute(
        f"CREATE TEMP VIEW input_{table} AS SELECT * FROM read_parquet("
        f"{literal_list(files)}, union_by_name=true, hive_partitioning={'true' if table == 'agent_events' else 'false'}, "
        "filename=true, file_row_number=true)"
    )
    observed = {r[0] for r in con.execute(f"DESCRIBE input_{table}").fetchall()}
    unknown = observed - schema.keys() - {"filename", "file_row_number"}
    if unknown:
        raise LakeError("rebuild_unknown_schema_columns")
    projection = ", ".join(
        (
            f'CAST("{c}" AS {t}) AS "{c}"'
            if c in observed
            else f'CAST(NULL AS {t}) AS "{c}"'
        )
        for c, t in schema.items()
    )
    # Relative archive path + physical file ordinal distinguishes identical null
    # key rows without inventing canonical business identities.
    con.execute(
        f"CREATE TEMP VIEW normalized_{table} AS SELECT {projection}, "
        f"substr(filename, {len(str(extracted)) + 2}) AS _import_file, "
        f"file_row_number AS _import_row, filename AS _physical_file FROM input_{table}"
    )
    select = f"SELECT * EXCLUDE (_physical_file), {row_hash_expression(schema)} AS _row_sha256 FROM normalized_{table}"
    con.execute(f"CREATE TABLE raw_{table} AS {select} LIMIT 0")
    # Every slice uses the same globally bound relation and declared CASTs.
    # Slice only execution, never type inference (mixed-era timestamps retain
    # the globally unified VARCHAR representation).
    for offset in range(0, len(files), 20):
        selected = ", ".join(literal(f) for f in files[offset : offset + 20])
        con.execute(
            f"INSERT INTO raw_{table} {select} WHERE _physical_file IN ({selected})"
        )


def literal_list(values: list[str]) -> str:
    return "[" + ", ".join(literal(v) for v in values) + "]"


def dedupe_events(con):
    # Preserve repository ordering; normalized payload SHA and file/ordinal make
    # exact attribution/timestamp/id ties deterministic across replay/batching.
    con.execute("""
        CREATE TABLE event_winners AS
        SELECT _import_file, _import_row, dedup_key, _row_sha256,
          CASE WHEN dedup_key IS NULL THEN 1 ELSE row_number() OVER (
            PARTITION BY dedup_key
            ORDER BY (repo_owner IS NOT NULL AND repo_name IS NOT NULL) DESC,
                     TRY_CAST(timestamp AS TIMESTAMPTZ) DESC NULLS LAST,
                     id DESC NULLS LAST, _row_sha256 DESC,
                     _import_file, _import_row
          ) END AS winner_rank
        FROM raw_agent_events
    """)
    con.execute("""
        CREATE VIEW canonical_events AS
        SELECT raw.* FROM raw_agent_events raw
        JOIN event_winners w USING (_import_file, _import_row)
        WHERE w.winner_rank = 1
    """)


def accounting(con, relation: str, *, expression: str = "_row_sha256") -> dict:
    count = con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]
    return {
        "rows": count,
        "normalized_multiset_sha256": hash_multiset(
            con, relation, expression=expression
        ),
    }


def rebuild(source: Path, spec: LakeSpec, *, dry_run: bool = False) -> dict:
    started = time.monotonic()
    verify_runtime(spec)
    root = spec.data_root.resolve()
    if root.exists():
        raise LakeError("rebuild_requires_new_data_root")
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(mode=0o700)
    evidence = root / "verification"
    evidence.mkdir()
    extracted = root / "frozen"
    inventory = extract_frozen(source, extracted)
    (evidence / "source-files.json").write_text(json.dumps(inventory, indent=2))
    scratch = root / "staging.duckdb"
    with duckdb.connect(
        str(scratch),
        config={
            "memory_limit": "512MB",
            "preserve_insertion_order": False,
            "threads": 2,
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
        },
    ) as staging:
        staging.execute("SET TimeZone = 'UTC'")
        for table, files in inventory.items():
            stage_relation(staging, table, files, extracted)
        dedupe_events(staging)
        raw = {t: accounting(staging, "raw_" + t) for t in SCHEMAS}
        canonical = accounting(staging, "canonical_events")
        nulls, keys = staging.execute(
            "SELECT count(*) FILTER (WHERE dedup_key IS NULL), count(DISTINCT dedup_key) FROM canonical_events"
        ).fetchone()
        for grain in ("date", "session_id"):
            staging.execute(
                f"COPY (SELECT raw.{grain}, count(*) AS raw_rows, count(*) FILTER (WHERE w.winner_rank = 1) AS canonical_rows FROM raw_agent_events raw JOIN event_winners w USING (_import_file, _import_row) GROUP BY raw.{grain} ORDER BY raw.{grain}) TO {literal(str(evidence / (grain + '-counts.parquet')))} (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        staging.execute(
            f"""COPY (
            SELECT loser.dedup_key, loser._import_file AS loser_file, loser._import_row AS loser_row,
                   loser._row_sha256 AS loser_sha256, winner._import_file AS winner_file,
                   winner._import_row AS winner_row, winner._row_sha256 AS winner_sha256
              FROM event_winners loser JOIN event_winners winner ON loser.dedup_key = winner.dedup_key
             WHERE loser.winner_rank > 1 AND winner.winner_rank = 1
             ORDER BY loser.dedup_key, loser_file, loser_row
        ) TO {literal(str(evidence / 'winner-loser.parquet'))} (FORMAT PARQUET, COMPRESSION ZSTD)"""
        )
        if not dry_run:
            # This is an explicit offline creation, not migration of an existing
            # catalog. Never attach before checking that the database is empty.
            with drained_mutation(spec.dsn()) as fence:
                tables = fence.connection.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
                ).fetchone()[0]
                if tables:
                    raise LakeError("rebuild_requires_fresh_catalog")
                attach_lake(staging, spec, read_only=False, create=True)
                with nullcontext(staging) as lake:
                    configure_catalog(lake)
                    for table, schema in SCHEMAS.items():
                        create_table(
                            lake,
                            table,
                            schema
                            | {
                                "_import_file": "VARCHAR",
                                "_import_row": "BIGINT",
                                "_row_sha256": "VARCHAR",
                            },
                            day_partition=table == "agent_events",
                        )
                    lake.execute("BEGIN")
                    try:
                        for table in SCHEMAS:
                            source_relation = (
                                "canonical_events"
                                if table == "agent_events"
                                else "raw_" + table
                            )
                            lake.execute(
                                f"INSERT INTO lake.{table} BY NAME SELECT * FROM staging.{source_relation}"
                            )
                        fence.check()
                        lake.execute("COMMIT")
                    except Exception:
                        lake.execute("ROLLBACK")
                        raise
                    actual = {
                        t: accounting(
                            lake,
                            "lake." + t,
                            expression=row_hash_expression(SCHEMAS[t]),
                        )
                        for t in SCHEMAS
                    }
                    expected = {**raw, "agent_events": canonical}
                    if actual != expected:
                        raise LakeError("rebuild_verification_mismatch")
        report = {
            "format_version": 1,
            "dry_run": dry_run,
            "raw": raw,
            "canonical_agent_events": canonical,
            "losers": raw["agent_events"]["rows"] - canonical["rows"],
            "null_key_rows_retained": nulls,
            "distinct_nonnull_keys": keys,
            "elapsed_seconds": time.monotonic() - started,
        }
        report["evidence_sha256"] = {
            p.name: sha256_file(p) for p in sorted(evidence.iterdir()) if p.is_file()
        }
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        return report


def verify(spec: LakeSpec) -> dict:
    report_path = spec.data_root / "verification/report.json"
    if not report_path.is_file():
        raise LakeError("lake_verification_baseline_missing")
    report = json.loads(report_path.read_text())
    if report["dry_run"]:
        raise LakeError("lake_dry_run_not_published")
    expected = {**report["raw"], "agent_events": report["canonical_agent_events"]}
    with lake_connection(spec) as con:
        actual = {
            t: accounting(con, "lake." + t, expression=row_hash_expression(SCHEMAS[t]))
            for t in SCHEMAS
        }
        if actual != expected:
            raise LakeError("lake_verification_mismatch")
        counts = con.execute(
            "SELECT count(*), count(DISTINCT dedup_key), count(*) FILTER (WHERE dedup_key IS NULL) FROM lake.agent_events"
        ).fetchone()
        if counts[0] != counts[1] + counts[2]:
            raise LakeError("lake_duplicate_canonical_keys")
    for name, digest in report["evidence_sha256"].items():
        if Path(name).name != name or sha256_file(report_path.parent / name) != digest:
            raise LakeError("lake_verification_evidence_mismatch")
    return actual
