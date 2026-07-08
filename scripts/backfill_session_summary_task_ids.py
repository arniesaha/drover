#!/usr/bin/env python3
"""Backfill missing task_id values on session_summaries from agent_events.

The summarizer wrote session_summaries before repo/task attribution worked,
leaving session_summaries.task_id NULL even though agent_events for the same
session_id later gained proper task_ids. This script joins them and fills in
the gaps so handoff queries can find the summaries.

Idempotent: only touches rows where session_summaries.task_id IS NULL, and
only sets a value when at least one agent_events row for that session has a
non-null task_id.

Usage:
    uv run scripts/backfill_session_summary_task_ids.py [--dry-run] [--duckdb-path PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb


COUNT_SQL = """
SELECT COUNT(*)
FROM session_summaries ss
WHERE ss.task_id IS NULL
  AND EXISTS (
    SELECT 1 FROM agent_events ae
    WHERE ae.session_id = ss.session_id AND ae.task_id IS NOT NULL
  )
"""

UPDATE_SQL = """
UPDATE session_summaries AS ss
SET task_id = (
    SELECT any_value(ae.task_id)
    FROM agent_events ae
    WHERE ae.session_id = ss.session_id AND ae.task_id IS NOT NULL
)
WHERE ss.task_id IS NULL
  AND EXISTS (
    SELECT 1 FROM agent_events ae
    WHERE ae.session_id = ss.session_id AND ae.task_id IS NOT NULL
  )
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many rows would be updated without writing",
    )
    parser.add_argument(
        "--duckdb-path",
        default=str(Path.home() / ".nexus/nexus.duckdb"),
        help="Path to the nexus DuckDB file (default: ~/.nexus/nexus.duckdb)",
    )
    args = parser.parse_args()

    db_path = Path(args.duckdb_path).expanduser()
    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist", file=sys.stderr)
        sys.exit(1)

    con = duckdb.connect(str(db_path), read_only=args.dry_run)
    try:
        (candidate_count,) = con.execute(COUNT_SQL).fetchone()
        if args.dry_run:
            print(f"would update {candidate_count} rows")
            return

        con.execute(UPDATE_SQL)
        # DuckDB UPDATE doesn't return rowcount reliably across versions; the
        # candidate count above is the authoritative number since the WHERE
        # clause is identical and the UPDATE is atomic.
        print(f"updated {candidate_count} rows")
    finally:
        con.close()


if __name__ == "__main__":
    main()
