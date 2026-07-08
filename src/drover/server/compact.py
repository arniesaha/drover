"""Combine small Parquet files within a single partition.

Each partition (e.g. ``agent_events/date=2026-05-09/agent_id=claude/``)
accumulates many small ``part-*.parquet`` files because every ingest
call writes a new one. ``compact_partition`` reads them all, optionally
de-duplicates on a key column, and rewrites a single file.

The function is partition-shaped, not table-shaped — callers iterate
over partitions to do whole-table compaction.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger("drover.compact")


@dataclass(frozen=True)
class CompactResult:
    files_before: int
    files_after: int
    rows: int


def compact_partition(
    partition_dir: Path, *, dedup_column: Optional[str] = None
) -> CompactResult:
    files = sorted(partition_dir.glob("*.parquet"))
    if not files:
        return CompactResult(files_before=0, files_after=0, rows=0)
    if len(files) == 1 and dedup_column is None:
        # Single file, nothing to do unless we'd dedup
        rows = pq.read_metadata(files[0]).num_rows
        return CompactResult(files_before=1, files_after=1, rows=rows)

    # Read each file via ParquetFile (avoids the dataset auto-merge that
    # chokes on string-vs-dictionary inconsistencies). Cast every result
    # to a unified non-dictionary schema before concat.
    tables = [pq.ParquetFile(f).read() for f in files]
    target_schema = _unify_schema([t.schema for t in tables])
    normalized = [t.cast(target_schema) for t in tables]
    combined = pa.concat_tables(normalized)

    if dedup_column and dedup_column in combined.schema.names:
        combined = _dedup(combined, dedup_column)

    out_path = partition_dir / f"part-compact-{uuid.uuid4().hex[:8]}.parquet"
    pq.write_table(combined, out_path, compression="zstd")

    # Remove the originals only after the new file is on disk
    for f in files:
        f.unlink()

    final_files = list(partition_dir.glob("*.parquet"))
    rows = combined.num_rows
    log.info(
        "compacted %s: %d → %d files, %d rows",
        partition_dir,
        len(files),
        len(final_files),
        rows,
    )
    return CompactResult(
        files_before=len(files), files_after=len(final_files), rows=rows
    )


def _unify_schema(schemas: list[pa.Schema]) -> pa.Schema:
    """Build one schema with non-dictionary types for every field across inputs."""
    fields: dict[str, pa.DataType] = {}
    for schema in schemas:
        for f in schema:
            t = f.type
            if pa.types.is_dictionary(t):
                t = t.value_type
            fields.setdefault(f.name, t)
    return pa.schema([pa.field(name, t) for name, t in fields.items()])


def _dedup(table: pa.Table, key_col: str) -> pa.Table:
    """Keep first occurrence of each key, ignoring rows where the key is NULL."""
    keys = table.column(key_col).to_pylist()
    seen: set = set()
    keep: list[bool] = []
    for k in keys:
        if k is None:
            keep.append(True)
            continue
        if k in seen:
            keep.append(False)
        else:
            seen.add(k)
            keep.append(True)
    mask = pa.array(keep, type=pa.bool_())
    return table.filter(mask)


def compact_table(parquet_dir: Path, *, dedup_column: Optional[str] = None) -> dict:
    """Compact every leaf partition under ``parquet_dir``."""
    parquet_dir = Path(parquet_dir)
    results: list[CompactResult] = []
    # Leaf partitions are dirs that contain *.parquet files directly
    for d in parquet_dir.rglob("*"):
        if not d.is_dir():
            continue
        if not any(d.glob("*.parquet")):
            continue
        results.append(compact_partition(d, dedup_column=dedup_column))
    return {
        "partitions": len(results),
        "files_before": sum(r.files_before for r in results),
        "files_after": sum(r.files_after for r in results),
        "rows": sum(r.rows for r in results),
    }
