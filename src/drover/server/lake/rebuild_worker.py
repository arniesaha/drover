"""One offline partition per OS process; no engine or allocator survives a job."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

from .rebuild import (
    EVENT_SCHEMA,
    SCHEMAS,
    accounting,
    dedupe_events,
    hash_multiset,
    literal,
    literal_list,
    row_hash_expression,
    stage_relation,
)
from .runtime import LakeError, LakeSpec, attach_lake
from .runtime import literal as sql_literal

LINEAGE = {"_import_file": "VARCHAR", "_import_row": "BIGINT", "_row_sha256": "VARCHAR"}
POLICY_SCHEMA = EVENT_SCHEMA | {
    "dedup_key_source": "VARCHAR",
    "_source_dedup_key": "VARCHAR",
}


def _copy(con, sql: str, path: Path):
    con.execute(
        f"COPY ({sql}) TO {literal(str(path))} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 2048)"
    )


def _session_counts(con, partition, serving, archive):
    # Only session keys/counts are read. Every raw row belongs to serving,
    # archive, or a dedupe loser; NULL session IDs remain a separate bucket.
    raw = f"read_parquet({literal(str(partition / 'sessions.parquet'))})"
    _copy(
        con,
        f"""WITH served AS (SELECT session_id,count(*) AS serving_rows FROM {serving} GROUP BY session_id),
        archived AS (SELECT session_id,count(*) AS archive_rows FROM {archive} GROUP BY session_id)
        SELECT r.session_id,r.raw_rows,coalesce(s.serving_rows,0) AS serving_rows,
          coalesce(a.archive_rows,0) AS archive_rows,
          r.raw_rows-coalesce(s.serving_rows,0)-coalesce(a.archive_rows,0) AS loser_rows
        FROM {raw} r LEFT JOIN served s ON r.session_id IS NOT DISTINCT FROM s.session_id
        LEFT JOIN archived a ON r.session_id IS NOT DISTINCT FROM a.session_id""",
        partition / "session-counts.parquet",
    )


def _events(con, job: dict) -> dict:
    stage_relation(con, "agent_events", job["files"], Path(job["extracted"]))
    day = job["day"]
    if con.execute(
        "SELECT count(*) FROM raw_agent_events WHERE date IS NULL OR date != ?", [day]
    ).fetchone()[0]:
        raise LakeError("rebuild_partition_date_mismatch")
    # Baseline original-key accounting remains independent of policy changes.
    dedupe_events(con)
    raw_count = con.execute("SELECT count(*) FROM raw_agent_events").fetchone()[0]
    baseline = con.execute("SELECT count(*) FROM canonical_events").fetchone()[0]
    out = Path(job["output"])
    _copy(
        con,
        "SELECT _row_sha256 AS digest FROM raw_agent_events",
        out / "raw-hashes.parquet",
    )
    _copy(
        con,
        "SELECT _row_sha256 AS digest FROM canonical_events",
        out / "baseline-hashes.parquet",
    )
    import pyarrow as pa

    from drover.dedup import make_dedup_key

    con.execute(
        "CREATE TABLE backfilled_keys (_import_file VARCHAR, _import_row BIGINT, key VARCHAR)"
    )
    cursor = con.cursor()
    try:
        cursor.execute(
            "SELECT _import_file,_import_row,timestamp,agent_id,session_id,event_type,left(content,200) FROM raw_agent_events WHERE dedup_key IS NULL AND coalesce(content,'') != ''"
        )
        while rows := cursor.fetchmany(128):
            batch = pa.Table.from_pylist(
                [
                    {
                        "_import_file": file,
                        "_import_row": ordinal,
                        "key": make_dedup_key(*fields),
                    }
                    for file, ordinal, *fields in rows
                ]
            )
            con.register("backfill_batch", batch)
            con.execute("INSERT INTO backfilled_keys SELECT * FROM backfill_batch")
            con.unregister("backfill_batch")
    finally:
        cursor.close()
    con.execute(
        """CREATE TABLE policy_events AS SELECT raw.* EXCLUDE (dedup_key),
      dedup_key AS _source_dedup_key,
      CASE WHEN dedup_key IS NOT NULL THEN 'original'
           WHEN coalesce(content,'') != '' THEN 'rebuild_backfill'
           WHEN role IS NULL THEN 'legacy_metadata' ELSE 'legacy_null' END AS dedup_key_source,
      CASE WHEN dedup_key IS NULL AND coalesce(content,'') != ''
           THEN b.key
           ELSE dedup_key END AS dedup_key
      FROM raw_agent_events raw LEFT JOIN backfilled_keys b USING (_import_file,_import_row)"""
    )
    buckets = dict(
        con.execute(
            "SELECT dedup_key_source, count(*) FROM policy_events GROUP BY dedup_key_source"
        ).fetchall()
    )
    for bucket in ("original", "rebuild_backfill", "legacy_metadata", "legacy_null"):
        buckets.setdefault(bucket, 0)
    _copy(
        con,
        "SELECT dedup_key, _source_dedup_key, date FROM policy_events WHERE dedup_key IS NOT NULL",
        out / "keys.parquet",
    )
    con.execute("""CREATE TABLE policy_winners AS SELECT _import_file,_import_row,
      CASE WHEN dedup_key IS NULL THEN 1 ELSE row_number() OVER (PARTITION BY dedup_key
        ORDER BY (repo_owner IS NOT NULL AND repo_name IS NOT NULL) DESC,
          TRY_CAST(timestamp AS TIMESTAMPTZ) DESC NULLS LAST, id DESC NULLS LAST,
          _row_sha256 DESC, _import_file, _import_row) END AS winner_rank
      FROM policy_events WHERE dedup_key_source != 'legacy_metadata'""")
    con.execute(
        """CREATE VIEW serving AS SELECT p.* EXCLUDE (_row_sha256),
      """
        + row_hash_expression(POLICY_SCHEMA)
        + """ AS _row_sha256
      FROM policy_events p JOIN policy_winners w USING (_import_file,_import_row)
      WHERE winner_rank = 1"""
    )
    con.execute(
        "CREATE VIEW archive AS SELECT * EXCLUDE (_row_sha256), "
        + row_hash_expression(POLICY_SCHEMA)
        + " AS _row_sha256 FROM policy_events WHERE dedup_key_source='legacy_metadata'"
    )
    _copy(con, "SELECT * FROM serving", out / "events.parquet")
    _copy(con, "SELECT * FROM archive", out / "metadata.parquet")
    _copy(
        con,
        "SELECT session_id, count(*) AS raw_rows FROM raw_agent_events GROUP BY session_id",
        out / "sessions.parquet",
    )
    _session_counts(con, out, "serving", "archive")
    _copy(
        con,
        "SELECT dedup_key_source, count(*) AS rows FROM serving GROUP BY dedup_key_source",
        out / "served-buckets.parquet",
    )
    _copy(
        con,
        """SELECT p.dedup_key,p._import_file AS loser_file,p._import_row AS loser_row,
        p._row_sha256 AS loser_sha256, winner._import_file AS winner_file,
        winner._import_row AS winner_row,winner._row_sha256 AS winner_sha256
        FROM policy_events p JOIN policy_winners w USING (_import_file,_import_row)
        JOIN serving winner ON p.dedup_key=winner.dedup_key WHERE w.winner_rank>1""",
        out / "losers.parquet",
    )
    return {
        "day": day,
        "raw_rows": raw_count,
        "baseline_canonical_rows": baseline,
        "buckets": buckets,
        "serving": accounting(con, "serving"),
        "archive": accounting(con, "archive"),
    }


def _other(con, job):
    table = job["table"]
    stage_relation(con, table, job["files"], Path(job["extracted"]))
    out = Path(job["output"]) / "rows.parquet"
    _copy(con, f"SELECT * FROM raw_{table}", out)
    return accounting(con, "raw_" + table)


def _aggregate(con, job):
    parts = job["partitions"]
    out = Path(job["output"])
    keys = literal_list([str(Path(p) / "keys.parquet") for p in parts])
    # External sort streams only keys/day, never event payloads. Check both
    # original and rebuilt identities before a catalog is initialized.
    for column in ("dedup_key", "_source_dedup_key"):
        cursor = con.execute(
            f"SELECT {column}, date FROM read_parquet({keys}) WHERE {column} IS NOT NULL ORDER BY {column}, date"
        )
        previous = None
        previous_day = None
        while rows := cursor.fetchmany(2048):
            for key, day in rows:
                if key == previous and day != previous_day:
                    raise LakeError("rebuild_cross_partition_dedup_key")
                previous, previous_day = key, day
    hashes = {}
    for name in ("raw", "baseline"):
        paths = literal_list([str(Path(p) / (name + "-hashes.parquet")) for p in parts])
        con.execute(
            f"CREATE TEMP VIEW hashes_{name} AS SELECT * FROM read_parquet({paths})"
        )
        hashes[name] = hash_multiset(con, "hashes_" + name, expression="digest")
    for table, filename in (
        ("agent_events", "events.parquet"),
        ("agent_events_legacy_metadata", "metadata.parquet"),
    ):
        paths = literal_list([str(Path(p) / filename) for p in parts])
        con.execute(f"CREATE TEMP VIEW {table} AS SELECT * FROM read_parquet({paths})")
        hashes[table] = accounting(con, table)
    others = {}
    for table, files in job["others"].items():
        con.execute(
            f"CREATE TEMP VIEW other_{table} AS SELECT * FROM read_parquet({literal_list(files)})"
        )
        others[table] = accounting(con, "other_" + table)
    return {"hashes": hashes, "others": others, "cross_partition_check": "passed"}


def _publish(con, job):
    spec = _spec(job["spec"])
    attach_lake(con, spec, read_only=False)
    for table, files in job["tables"].items():
        con.execute(
            f"INSERT INTO lake.{table} BY NAME SELECT * FROM read_parquet({literal_list(files)}, hive_partitioning=false)"
        )
    return {"published": True}


def _verify(con, job):
    spec = _spec(job["spec"])
    attach_lake(con, spec, read_only=True)
    table = job["table"]
    schema = (
        POLICY_SCHEMA
        if table in {"agent_events", "agent_events_legacy_metadata"}
        else SCHEMAS[table]
    )
    condition = " WHERE date = " + literal(job["day"]) if "day" in job else ""
    con.execute(
        f"CREATE TEMP VIEW verified AS SELECT *, {row_hash_expression(schema)} AS _actual_hash FROM lake.{table}{condition}"
    )
    if con.execute(
        "SELECT count(*) FROM verified WHERE _row_sha256 != _actual_hash"
    ).fetchone()[0]:
        raise LakeError("lake_verification_mismatch")
    _copy(
        con,
        "SELECT _actual_hash AS digest FROM verified",
        Path(job["output"]) / "hashes.parquet",
    )
    if table == "agent_events" and "day" in job:
        partition = spec.data_root / "partitions" / job["day"]
        if not (partition / "session-counts.parquet").is_file():
            _session_counts(
                con,
                partition,
                f"(SELECT session_id FROM lake.agent_events{condition})",
                f"(SELECT session_id FROM lake.agent_events_legacy_metadata{condition})",
            )
    return {"rows": con.execute("SELECT count(*) FROM verified").fetchone()[0]}


def _verify_aggregate(con, job):
    con.execute(
        f"CREATE TEMP VIEW hashes AS SELECT * FROM read_parquet({literal_list(job['files'])})"
    )
    return {
        "rows": con.execute("SELECT count(*) FROM hashes").fetchone()[0],
        "normalized_multiset_sha256": hash_multiset(con, "hashes", expression="digest"),
    }


def _spec(value):
    return LakeSpec(
        **{
            **value,
            "data_root": Path(value["data_root"]),
            "extension_dir": Path(value["extension_dir"]),
        }
    )


def work(job):
    output = Path(job["output"])
    output.mkdir(parents=True, exist_ok=True)
    spill = Path(job["spill"])
    spill.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(
        str(output / "scratch.duckdb"),
        config={
            "memory_limit": "1GB",
            "threads": 1,
            "temp_directory": str(spill),
            "preserve_insertion_order": False,
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
        },
    ) as con:
        con.execute("SET TimeZone = 'UTC'")
        operation = {
            "events": _events,
            "other": _other,
            "aggregate": _aggregate,
            "publish": _publish,
            "verify": _verify,
            "verify_aggregate": _verify_aggregate,
        }[job["operation"]]
        return operation(con, job)


if __name__ == "__main__":
    request = json.loads(Path(sys.argv[1]).read_text())
    try:
        result = work(request)
    except LakeError as exc:
        result = {"error": exc.code}
    except duckdb.OutOfMemoryException:
        result = {"error": "rebuild_engine_memory_limit_exceeded"}
    Path(request["reply"]).write_text(json.dumps(result))
