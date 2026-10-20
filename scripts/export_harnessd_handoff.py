#!/usr/bin/env python3
"""Export a stopped Python host's local registry, without modifying DuckDB.

The orchestrator drains and stops the service first. DuckDB's read-only open
refuses a concurrently held writer lock. Rust never opens this database.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import duckdb


def rows(connection, query, params=()):
    result = connection.execute(query, params)
    columns = [item[0] for item in result.description]
    while batch := result.fetchmany(100):
        yield from (dict(zip(columns, row)) for row in batch)


def export(registry: Path, host_id: str, output: Path) -> dict[str, int]:
    if output.exists():
        raise ValueError("handoff output already exists; use a new path")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".partial")
    counts: dict[str, int] = {}
    with duckdb.connect(str(registry), read_only=True) as connection:
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        connection.execute("BEGIN TRANSACTION")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:

                def emit(kind, record):
                    stream.write(
                        json.dumps(
                            {"kind": kind, **record},
                            default=lambda v: (
                                v.isoformat() if isinstance(v, datetime) else str(v)
                            ),
                        )
                        + "\n"
                    )
                    counts[kind] = counts.get(kind, 0) + 1

                for row in rows(
                    connection,
                    "SELECT * FROM harness_sessions WHERE host_id=? ORDER BY session_id",
                    [host_id],
                ):
                    emit("session", {"session": row})
                # Include quarantined originals even if old software marked
                # them reconciled. Preserve their receipt after the event rows.
                for row in rows(
                    connection,
                    """
                    SELECT e.* FROM harness_events e
                    LEFT JOIN harness_sessions s ON s.session_id=e.session_id
                    WHERE (s.host_id=? OR s.session_id IS NULL)
                    AND (e.reconciled_at IS NULL OR EXISTS (
                        SELECT 1 FROM harness_event_quarantine q WHERE q.event_id=e.event_id))
                    ORDER BY e.created_at,e.event_id
                """,
                    [host_id],
                ):
                    envelope = json.loads(row["payload_json"])
                    if (
                        not isinstance(envelope, dict)
                        or row["seq"] is None
                        or not row["dedup_key"]
                    ):
                        raise ValueError(
                            "event lacks a complete identity; reconcile/migrate Python before export"
                        )
                    emit(
                        "event",
                        {
                            **envelope,
                            "event_id": row["event_id"],
                            "session_id": row["session_id"],
                            "dedup_key": row["dedup_key"],
                            "seq": row["seq"],
                            "type": envelope.get("type")
                            or row["normalized_type"]
                            or row["event_type"],
                            "ts": envelope.get("ts") or row["created_at"],
                        },
                    )
                for table, kind in [
                    ("session_worktrees", "worktree"),
                    ("session_lifecycle_operations", "lifecycle"),
                ]:
                    if table in tables:
                        for row in rows(
                            connection,
                            f"SELECT * FROM {table} WHERE host_id=?",
                            [host_id],
                        ):
                            emit(kind, {"record": row})
                for row in rows(
                    connection,
                    """SELECT q.* FROM harness_event_quarantine q
                    LEFT JOIN harness_sessions s ON s.session_id=q.session_id
                    WHERE s.host_id=? OR s.session_id IS NULL""",
                    [host_id],
                ):
                    emit("quarantine", {"record": row})
                stream.flush()
                os.fsync(stream.fileno())
            # Never replace an existing handoff, including a concurrent export.
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        connection.execute("ROLLBACK")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry", type=Path, required=True, help="Stopped host's *.registry.duckdb"
    )
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.registry, args.host_id, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
