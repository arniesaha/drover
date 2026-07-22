"""Tests for atomic parquet part-file writes (issue #10)."""

from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from drover.server.parquet_io import atomic_write_table


def _table() -> pa.Table:
    return pa.Table.from_pylist([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])


def test_atomic_write_produces_valid_parquet(tmp_path):
    out = tmp_path / "part-abc.parquet"
    atomic_write_table(_table(), out, compression="zstd")
    assert out.exists()
    assert pq.read_table(out).num_rows == 2


def test_no_partial_file_visible_to_glob_during_write(tmp_path):
    """While the bytes are being written, no *.parquet glob match may exist."""
    out = tmp_path / "part-abc.parquet"
    seen_during_write: list[str] = []

    real_write = pq.write_table

    def spy(table, where, **kwargs):
        # At the moment of the underlying write, readers globbing the
        # directory must not see any .parquet file (partial or otherwise).
        seen_during_write.extend(p.name for p in tmp_path.glob("*.parquet"))
        return real_write(table, where, **kwargs)

    with patch("drover.server.parquet_io.pq.write_table", side_effect=spy):
        atomic_write_table(_table(), out)

    assert seen_during_write == []
    assert out.exists()


def test_no_tmp_leftovers(tmp_path):
    out = tmp_path / "part-abc.parquet"
    atomic_write_table(_table(), out)
    leftovers = [p for p in tmp_path.iterdir() if p.name != out.name]
    assert leftovers == []


def test_tmp_file_removed_on_write_failure(tmp_path):
    out = tmp_path / "part-abc.parquet"

    with patch(
        "drover.server.parquet_io.pq.write_table",
        side_effect=OSError("disk full"),
    ):
        try:
            atomic_write_table(_table(), out)
        except OSError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected OSError to propagate")

    assert list(tmp_path.iterdir()) == []
