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


def _written(path: Path, ids: list[str]) -> Path:
    """`_write_parquet` that hands the path back, for building variants."""
    _write_parquet(path, ids)
    return path


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


def test_compact_partition_whose_files_have_different_columns(tmp_path: Path) -> None:
    """A column added part-way through the table's life must not block compaction.

    The live `spans` tree has exactly this shape: older files predate
    `agent_model` / `stop_reason`, newer ones carry them. `_unify_schema`
    already returns the union of every field, but `Table.cast` can only retype
    columns a table already has -- it cannot add the missing ones -- so
    compaction died on the first evolved partition with "Target schema's field
    names are not matching the table's field names".
    """
    partition = tmp_path / "evolved"
    partition.mkdir()
    _write_parquet(partition / "part-old.parquet", ["a", "b"])

    # A later writer adds a column the older file never had.
    newer = pq.ParquetFile(partition / "part-old.parquet").read()
    newer = newer.append_column(
        "stop_reason", pa.array(["end_turn", "max_tokens"], type=pa.string())
    )
    pq.write_table(newer, partition / "part-new.parquet")

    result = compact_partition(partition)

    assert result.files_before == 2
    assert result.files_after == 1
    assert result.rows == 4

    table = pq.ParquetFile(next(partition.glob("*.parquet"))).read()
    assert "stop_reason" in table.schema.names
    # The rows that predate the column read back as NULL rather than vanishing.
    assert sorted(
        v or "" for v in table.column("stop_reason").to_pylist()
    ) == ["", "", "end_turn", "max_tokens"]
    assert sorted(table.column("id").to_pylist()) == ["a", "a", "b", "b"]


def test_compact_prefers_a_real_type_over_an_all_null_column(tmp_path: Path) -> None:
    """A column that was all-NULL when first written must not pin the union to `null`.

    Parquet records an all-NULL column as type `null`. `_unify_schema` kept
    the first type it saw per field, so if the older file in a partition had
    the column empty and a later one carried strings, the unified target was
    `null` and conforming the newer table died with "Unsupported cast from
    string to null". Found against the live tree, not synthetically.
    """
    partition = tmp_path / "nulltyped"
    partition.mkdir()

    base = pq.ParquetFile(_written(partition / "seed.parquet", ["a"])).read()
    (partition / "seed.parquet").unlink()

    # Names chosen so the all-NULL file sorts FIRST: compact_partition reads
    # in sorted order, and the bug only bites when `null` is the type seen
    # first. Reversed, the union already picks up `string` and nothing fails.
    empty = base.append_column("stop_reason", pa.nulls(1, type=pa.null()))
    pq.write_table(empty, partition / "part-aaa-empty.parquet")

    filled = base.append_column("stop_reason", pa.array(["end_turn"], type=pa.string()))
    pq.write_table(filled, partition / "part-bbb-filled.parquet")

    result = compact_partition(partition)

    assert result.rows == 2
    table = pq.ParquetFile(next(partition.glob("*.parquet"))).read()
    assert pa.types.is_string(table.schema.field("stop_reason").type)
    assert sorted(v or "" for v in table.column("stop_reason").to_pylist()) == [
        "",
        "end_turn",
    ]


def test_compact_dedups_when_dedup_column_present(tmp_path: Path) -> None:
    partition = tmp_path / "dedupable"
    partition.mkdir()
    _write_parquet(partition / "part-x.parquet", ["a", "b"])
    _write_parquet(partition / "part-y.parquet", ["b", "c"])  # 'b' duplicated

    result = compact_partition(partition, dedup_column="dedup_key")
    table = pq.ParquetFile(next(partition.glob("*.parquet"))).read()
    assert sorted(table.column("dedup_key").to_pylist()) == ["a", "b", "c"]
    assert result.rows == 3
