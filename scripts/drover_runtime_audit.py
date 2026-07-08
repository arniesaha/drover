#!/usr/bin/env python3
"""Read-only operational audit for Drover runtime state.

Usage:
    python scripts/drover_runtime_audit.py --db ~/.drover/drover.duckdb --incoming-dir ~/.drover/incoming
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from drover.server.doctor import format_runtime_audit, runtime_audit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Drover runtime health audit")
    parser.add_argument("--db", required=True, type=Path, help="DuckDB database path")
    parser.add_argument(
        "--incoming-dir", type=Path, default=None, help="Incoming directory to scan"
    )
    parser.add_argument(
        "--hours", type=int, default=24, help="Repo attribution lookback window"
    )
    args = parser.parse_args()

    report = runtime_audit(
        duckdb_path=args.db, incoming_dir=args.incoming_dir, hours=args.hours
    )
    print(format_runtime_audit(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
