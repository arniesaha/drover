#!/usr/bin/env python3
"""Read-only Phase 2 audit. Never bootstraps, repairs, enqueues, or writes stores.

Example (run later on the hub):
  uv run python scripts/memory_acceptance.py --duckdb-path /path/to/db harness-...
The analytical identity projection is sufficient; no control write credentials
are needed. A locked/missing store is reported as unavailable, never opened RW.
For a running hub (which owns the DuckDB writer lock), use its MCP endpoint:
  uv run python scripts/memory_acceptance.py --mcp-url http://hub:8080/mcp harness-...
The remote audit tool only reads; this script never requests mutations.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import requests

from drover.server.memory_audit import audit_session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--duckdb-path", type=Path)
    source.add_argument("--mcp-url")
    parser.add_argument("harness_ids", nargs="+")
    args = parser.parse_args()
    if args.mcp_url:
        from drover.server.mcp.client import DroverMCPClientError, call_tool

        try:
            reports = []
            for start in range(0, len(args.harness_ids), 25):
                result = call_tool(
                    args.mcp_url,
                    "drover_memory_acceptance",
                    {"harness_ids": args.harness_ids[start : start + 25]},
                )
                if result.get("isError"):
                    raise DroverMCPClientError("audit read failed")
                structured = result.get("structuredContent")
                if structured:
                    payload = structured.get("result", structured)
                else:
                    payload = json.loads(
                        next(
                            item["text"]
                            for item in result.get("content", [])
                            if item.get("type") == "text"
                        )
                    )
                reports.extend(payload["sessions"])
            print(json.dumps(reports, indent=2))
        except (
            DroverMCPClientError,
            requests.RequestException,
            ValueError,
            KeyError,
            StopIteration,
        ) as exc:
            print(
                json.dumps(
                    [
                        {"harness_id": sid, "status": "unavailable", "reason": str(exc)}
                        for sid in args.harness_ids
                    ]
                )
            )
        return
    try:
        con = duckdb.connect(str(args.duckdb_path), read_only=True)
    except duckdb.Error as exc:
        print(
            json.dumps(
                [
                    {"harness_id": sid, "status": "unavailable", "reason": str(exc)}
                    for sid in args.harness_ids
                ]
            )
        )
        return
    try:
        reports = []
        for sid in args.harness_ids:
            try:
                reports.append(audit_session(con, sid))
            except duckdb.Error as exc:
                reports.append(
                    {"harness_id": sid, "status": "unavailable", "reason": str(exc)}
                )
        print(json.dumps(reports, indent=2))
    finally:
        con.close()


if __name__ == "__main__":
    main()
