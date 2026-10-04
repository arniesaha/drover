"""Offline frozen-tar rebuild with a single declared schema and dedupe lineage."""

from __future__ import annotations

import hashlib
import shutil
import tarfile
from pathlib import Path, PurePosixPath

from .runtime import LakeError, LakeSpec, literal

# Schema is intentionally explicit, including VARCHAR event timestamps. Binding
# Every incoming batch is cast to these types before any hash or deduplication.
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
    """Stream fixed-schema record batches; never infer a batch's target types."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = SCHEMAS[table]
    ddl = ", ".join(f'"{c}" {t}' for c, t in schema.items())
    con.execute(
        f"CREATE TABLE raw_{table} ({ddl}, _import_file VARCHAR, _import_row BIGINT, _row_sha256 VARCHAR)"
    )
    files = [str(extracted / item["path"]) for item in inventory]
    # This audit-only binding checks the historical global representation in
    # tests. Execution below always casts into the declared schema.
    con.execute(
        f"CREATE TEMP VIEW input_{table} AS SELECT * FROM read_parquet({literal_list(files)}, union_by_name=true, hive_partitioning={'true' if table == 'agent_events' else 'false'})"
    )
    for item in inventory:
        path = extracted / item["path"]
        parquet = pq.ParquetFile(path)
        observed = set(parquet.schema_arrow.names)
        if observed - schema.keys():
            raise LakeError("rebuild_unknown_schema_columns")
        hive = dict(
            part.split("=", 1) for part in Path(item["path"]).parts if "=" in part
        )
        offset = 0
        for batch in parquet.iter_batches(batch_size=128, use_threads=False):
            incoming = pa.Table.from_batches([batch])
            if table == "agent_events":
                for column in ("date", "agent_id"):
                    if column not in hive:
                        raise LakeError("rebuild_missing_hive_identity")
                    values = pa.array([hive[column]] * batch.num_rows, type=pa.string())
                    if column in incoming.column_names:
                        incoming = incoming.set_column(
                            incoming.column_names.index(column), column, values
                        )
                    else:
                        incoming = incoming.append_column(column, values)
            incoming = incoming.append_column(
                "_file_ordinal",
                pa.array(range(offset, offset + batch.num_rows), type=pa.int64()),
            )
            offset += batch.num_rows
            columns = set(incoming.column_names)
            projection = ", ".join(
                (
                    f'CAST("{c}" AS {t}) AS "{c}"'
                    if c in columns
                    else f'CAST(NULL AS {t}) AS "{c}"'
                )
                for c, t in schema.items()
            )
            con.register("incoming_batch", incoming)
            try:
                con.execute(
                    f"INSERT INTO raw_{table} SELECT *, {row_hash_expression(schema)} AS _row_sha256 FROM (SELECT {projection}, {literal(item['path'])} AS _import_file, _file_ordinal AS _import_row FROM incoming_batch)"
                )
            finally:
                con.unregister("incoming_batch")


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
    from .partition_rebuild import rebuild_partitioned

    return rebuild_partitioned(source, spec, dry_run=dry_run)


def verify(spec: LakeSpec) -> dict:
    from .partition_rebuild import verify_partitioned

    return verify_partitioned(spec)
