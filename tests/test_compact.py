"""Tests for drover.server.compact.compact_partition."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from drover.server.compact import compact_partition


def _write_parquet(path: Path, ids: list[str]) -> None:
    schema = pa.schema(
        [
            ("id", pa.string()),
            ("session_id", pa.string()),
            ("agent_id", pa.string()),
            ("task_id", pa.string()),
            ("timestamp", pa.timestamp("us", tz="UTC")),
            ("event_type", pa.string()),
            ("role", pa.string()),
            ("content", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("principal_id", pa.string()),
            ("dedup_key", pa.string()),
            ("raw_data", pa.string()),
        ]
    )
    n = len(ids)
    cols = {f.name: [None] * n for f in schema}
    cols["id"] = ids
    cols["dedup_key"] = ids
    cols["timestamp"] = [datetime.now(timezone.utc)] * n
    table = pa.table(
        {k: pa.array(v, type=schema.field(k).type) for k, v in cols.items()},
        schema=schema,
    )
    pq.write_table(table, path)


def test_compact_combines_multiple_files_into_one(tmp_path: Path) -> None:
    partition = tmp_path / "agent_events" / "date=2026-05-09" / "agent_id=claude"
    partition.mkdir(parents=True)
    _write_parquet(partition / "part-aaa.parquet", ["a", "b"])
    _write_parquet(partition / "part-bbb.parquet", ["c"])
    _write_parquet(partition / "part-ccc.parquet", ["d", "e", "f"])

    result = compact_partition(partition)
    assert result.files_before == 3
    assert result.files_after == 1
    assert result.rows == 6

    files = sorted(partition.glob("*.parquet"))
    assert len(files) == 1
    table = pq.ParquetFile(files[0]).read()
    assert sorted(table.column("id").to_pylist()) == ["a", "b", "c", "d", "e", "f"]


def test_compact_noop_on_single_file(tmp_path: Path) -> None:
    partition = tmp_path / "p"
    partition.mkdir()
    _write_parquet(partition / "part.parquet", ["a"])
    result = compact_partition(partition)
    assert result.files_before == 1
    assert result.files_after == 1
    assert result.rows == 1


def test_compact_noop_on_empty_dir(tmp_path: Path) -> None:
    partition = tmp_path / "empty"
    partition.mkdir()
    result = compact_partition(partition)
    assert result.files_before == 0
    assert result.files_after == 0
    assert result.rows == 0


def test_compact_dedups_when_dedup_column_present(tmp_path: Path) -> None:
    partition = tmp_path / "dedupable"
    partition.mkdir()
    _write_parquet(partition / "part-x.parquet", ["a", "b"])
    _write_parquet(partition / "part-y.parquet", ["b", "c"])  # 'b' duplicated

    result = compact_partition(partition, dedup_column="dedup_key")
    table = pq.ParquetFile(next(partition.glob("*.parquet"))).read()
    assert sorted(table.column("dedup_key").to_pylist()) == ["a", "b", "c"]
    assert result.rows == 3
