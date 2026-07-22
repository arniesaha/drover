"""Atomic parquet part-file writes.

Writers append part-files into directories that concurrent readers scan via
``*.parquet`` globs (DuckDB views, diagnostics, tests). Writing directly to
the final name lets a reader observe a half-written file and fail with
"too small to be a Parquet file". Write to a ``.parquet.tmp`` name (invisible
to the glob) and rename into place — same pattern the collector uses for
``.jsonl.tmp`` → ``.jsonl``. Rename is atomic on the same filesystem.
"""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def atomic_write_table(table: pa.Table, out_path: Path, **write_kwargs) -> None:
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    try:
        pq.write_table(table, tmp_path, **write_kwargs)
        os.replace(tmp_path, out_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
