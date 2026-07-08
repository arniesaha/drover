"""Tests for drover.server.doctor.audit_lakehouse."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from drover.schema import bootstrap
from drover.server.doctor import audit_lakehouse


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _write_event_row(parquet_dir: Path, *, date: str, agent_id: str, n: int) -> None:
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
    cols = {f.name: [None] * n for f in schema}
    cols["id"] = [f"r{i}" for i in range(n)]
    cols["dedup_key"] = [f"k{i}" for i in range(n)]
    cols["timestamp"] = [datetime.now(timezone.utc)] * n
    table = pa.table(
        {k: pa.array(v, type=schema.field(k).type) for k, v in cols.items()},
        schema=schema,
    )
    out = parquet_dir / "agent_events" / f"date={date}" / f"agent_id={agent_id}"
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / "part-test.parquet")


def test_doctor_reports_zero_on_fresh_lakehouse(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    incoming = tmp_path / "incoming"
    report = audit_lakehouse(
        parquet_dir=parquet_dir, duckdb_path=duckdb_path, incoming_dir=incoming
    )
    assert report["agent_events_total"] == 0
    assert report["spans_total"] == 0
    assert report["warnings"] == []


def test_doctor_counts_per_partition(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_event_row(parquet_dir, date="2026-05-09", agent_id="macmini-claude", n=5)
    _write_event_row(parquet_dir, date="2026-05-09", agent_id="nas-claude", n=3)
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    report = audit_lakehouse(
        parquet_dir=parquet_dir,
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
    )
    assert report["agent_events_total"] == 8
    by = report["agent_events_by_partition"]
    assert by[("2026-05-09", "macmini-claude")] == 5
    assert by[("2026-05-09", "nas-claude")] == 3


def test_doctor_warns_when_processed_files_drift(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    incoming = tmp_path / "incoming" / "macmini" / ".processed"
    incoming.mkdir(parents=True)
    # 100 processed files for one host but zero parquet rows → drift
    for i in range(100):
        (incoming / f"r{i}.jsonl").write_text("{}\n")
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    report = audit_lakehouse(
        parquet_dir=parquet_dir,
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
    )
    assert any("macmini" in w for w in report["warnings"])
    assert report["processed_files"]["macmini"] == 100


def test_doctor_handles_missing_incoming_dir(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    report = audit_lakehouse(
        parquet_dir=parquet_dir,
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "nonexistent",
    )
    assert report["processed_files"] == {}
