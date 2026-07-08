#!/usr/bin/env python3
"""Backfill repo attribution into existing agent_events Parquet partitions.

For each partition file:
  - Reads rows missing repo_owner/repo_name/branch.
  - Runs enrich_raw_repo_attribution() on raw_data (uses git when cwd is locally accessible).
  - Re-writes the partition with enriched columns if anything changed.

Usage:
    uv run scripts/backfill_attribution.py [--dry-run] [--parquet-dir PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# Ensure src/ is on the path when run directly.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from drover.attribution import enrich_raw_repo_attribution  # noqa: E402


def _backfill_file(path: Path, *, dry_run: bool) -> tuple[int, int]:
    """Return (rows_read, rows_enriched). Rewrites file if anything changed."""
    try:
        table = pq.read_table(str(path))
    except Exception as exc:
        print(f"  SKIP {path.name}: {exc}", file=sys.stderr)
        return 0, 0

    if "raw_data" not in table.schema.names:
        return len(table), 0

    has_repo_owner = "repo_owner" in table.schema.names
    has_repo_name = "repo_name" in table.schema.names
    has_branch = "branch" in table.schema.names

    rows = table.to_pylist()
    enriched_count = 0
    changed = False

    for row in rows:
        # Skip rows that already have complete attribution.
        if (
            has_repo_owner and row.get("repo_owner")
            and has_repo_name and row.get("repo_name")
        ):
            continue

        rd_raw = row.get("raw_data")
        if not rd_raw:
            continue
        try:
            rd = json.loads(rd_raw) if isinstance(rd_raw, str) else rd_raw
        except (json.JSONDecodeError, TypeError):
            continue

        enriched = enrich_raw_repo_attribution(rd)
        owner = enriched.get("_repo_owner")
        name = enriched.get("_repo_name")
        br = enriched.get("gitBranch")

        if owner and not row.get("repo_owner"):
            row["repo_owner"] = owner
            changed = True
        if name and not row.get("repo_name"):
            row["repo_name"] = name
            changed = True
        if br and not row.get("branch"):
            row["branch"] = br
            changed = True

        if owner or name or br:
            enriched_count += 1

    if not changed:
        return len(rows), 0

    if dry_run:
        return len(rows), enriched_count

    # Update columns in-place using PyArrow column operations to avoid
    # type-coercion errors from from_pylist on complex schemas.
    for col, typ in [("repo_owner", pa.string()), ("repo_name", pa.string()), ("branch", pa.string())]:
        values = [r.get(col) for r in rows]
        arr = pa.array(values, type=typ)
        if col in table.schema.names:
            idx = table.schema.get_field_index(col)
            table = table.set_column(idx, col, arr)
        else:
            table = table.append_column(pa.field(col, typ), arr)

    pq.write_table(table, str(path), compression="zstd")
    return len(rows), enriched_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument(
        "--parquet-dir",
        default=str(Path.home() / ".nexus/parquet"),
        help="Root parquet directory (default: ~/.nexus/parquet)",
    )
    args = parser.parse_args()

    parquet_dir = Path(args.parquet_dir) / "agent_events"
    if not parquet_dir.exists():
        print(f"ERROR: {parquet_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    files = sorted(parquet_dir.glob("**/*.parquet"))
    print(f"Found {len(files)} Parquet files under {parquet_dir}")
    if args.dry_run:
        print("DRY RUN — no files will be written\n")

    total_rows = total_enriched = 0
    for f in files:
        rows, enriched = _backfill_file(f, dry_run=args.dry_run)
        total_rows += rows
        total_enriched += enriched
        if enriched:
            print(f"  {'+' if not args.dry_run else '~'} {f.parent.name}/{f.name}: {enriched}/{rows} enriched")

    print(f"\nDone. {total_enriched:,} / {total_rows:,} rows enriched.")
    if args.dry_run and total_enriched:
        print("Re-run without --dry-run to write changes.")


if __name__ == "__main__":
    main()
