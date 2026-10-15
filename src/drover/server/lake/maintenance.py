"""Explicit, fenced DuckLake file and snapshot maintenance."""

from __future__ import annotations

from time import monotonic

from .fence import drained_mutation
from .runtime import LakeSpec, lake_connection

EXPORT_MAINTENANCE_TABLES = (
    "agent_events",
    "activity_daily",
    "control_outbox_batches",
    "export_event_versions",
    "export_batch_receipts",
)
CLEANUP_RETENTION = "7 days"


def _file_count(con, table: str) -> int:
    row = con.execute(
        "SELECT coalesce(file_count, 0) FROM ducklake_table_info('lake') "
        "WHERE table_name = ?",
        [table],
    ).fetchone()
    return int(row[0]) if row else 0


def _snapshot_count(con) -> int:
    return int(con.execute("SELECT count(*) FROM lake.snapshots()").fetchone()[0])


def maintain_lake(
    spec: LakeSpec, *, tables: tuple[str, ...] = EXPORT_MAINTENANCE_TABLES
):
    """Compact one catalog under the drained writer/reader fence.

    Snapshot expiry and physical cleanup happen only after every table merge
    completes.  DuckLake's configured 30-day expiry and a separate seven-day
    file safety window retain both time-travel and long-running readers.
    """
    selected = tuple(dict.fromkeys(tables))
    if not selected or any(
        table not in EXPORT_MAINTENANCE_TABLES for table in selected
    ):
        raise ValueError("maintenance tables must be export tables")
    started = monotonic()
    with (
        drained_mutation(spec.dsn()) as fence,
        lake_connection(spec, read_only=False) as con,
    ):
        snapshots_before = _snapshot_count(con)
        report = {"tables": {}}
        for table in selected:
            before = _file_count(con, table)
            merged = con.execute(
                "CALL ducklake_merge_adjacent_files('lake', ?, schema => 'main')",
                [table],
            ).fetchall()
            report["tables"][table] = {
                "files_before": before,
                "merge_outputs": len(merged),
                "files_after": _file_count(con, table),
            }
            fence.check()
        con.execute("CALL ducklake_expire_snapshots('lake')")
        fence.check()
        con.execute(
            "CALL ducklake_cleanup_old_files('lake', "
            "older_than => now() - INTERVAL '7 days')"
        )
        fence.check()
        report.update(
            {
                "snapshots_before": snapshots_before,
                "snapshots_after": _snapshot_count(con),
                "cleanup_retention": CLEANUP_RETENTION,
                "elapsed_seconds": round(monotonic() - started, 3),
            }
        )
        return report
