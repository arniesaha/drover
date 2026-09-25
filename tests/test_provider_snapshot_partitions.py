"""Snapshots land in a dated partition, and closed days get compacted.

The tree was one flat directory: one file per write, ~1 per minute, 18,583
files by 2026-09-20. Creating the view over it read every footer and cost
325.9 s at every start (drover#382), and the writer's own duplicate guard
scanned the whole table on every write. Compaction fixed the symptom by hand;
partitioning plus a scheduled pass is what keeps it fixed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from drover.schema import bootstrap
from drover.server.providers.service import (
    ProviderUsageService,
    compact_closed_snapshot_partitions,
)
from drover.server.providers.types import ProviderAccountSnapshot


def _snapshot(suffix: str, observed_at: datetime) -> ProviderAccountSnapshot:
    return ProviderAccountSnapshot(
        snapshot_id=f"snapshot-{suffix}",
        dedup_key=f"dedup-{suffix}",
        provider="codex",
        account_label="Personal",
        plan_label=None,
        host_id="mac-mini",
        status="ok",
        observed_at=observed_at,
        windows=(),
        source="codex-cli",
    )


def _service(tmp_path: Path) -> ProviderUsageService:
    db = tmp_path / "drover.duckdb"
    parquet = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet, duckdb_path=db)
    return ProviderUsageService(duckdb_path=db, parquet_dir=parquet)


def _files(service: ProviderUsageService) -> list[Path]:
    """Only written snapshots: bootstrap leaves a `_seed` file behind."""
    return sorted(
        path
        for path in service.snapshot_dir.rglob("*.parquet")
        if path.parent.name.startswith("date=")
    )


def test_snapshots_are_written_into_a_dated_partition(tmp_path: Path) -> None:
    service = _service(tmp_path)
    when = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)

    service._persist_new_snapshots((_snapshot("a", when),), host_id="mac-mini")

    written = _files(service)
    assert len(written) == 1
    assert written[0].parent.name == "date=2026-09-18", written[0]


def test_snapshots_from_different_days_land_in_different_partitions(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    first = datetime(2026, 9, 18, 23, 50, tzinfo=timezone.utc)
    second = datetime(2026, 9, 19, 0, 10, tzinfo=timezone.utc)

    service._persist_new_snapshots(
        (_snapshot("a", first), _snapshot("b", second)), host_id="mac-mini"
    )

    partitions = sorted(p.parent.name for p in _files(service))
    assert partitions == ["date=2026-09-18", "date=2026-09-19"]


def test_every_row_is_still_readable_through_the_view(tmp_path: Path) -> None:
    service = _service(tmp_path)
    when = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
    service._persist_new_snapshots(
        (_snapshot("a", when), _snapshot("b", when + timedelta(days=1))),
        host_id="mac-mini",
    )
    con = duckdb.connect(str(service.duckdb_path))
    try:
        keys = {
            row[0]
            for row in con.execute(
                "SELECT dedup_key FROM provider_usage_snapshots"
            ).fetchall()
        }
    finally:
        con.close()
    assert keys == {"dedup-a", "dedup-b"}


def test_compaction_merges_closed_days_and_leaves_today_alone(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    today = datetime.now(timezone.utc)
    closed = today - timedelta(days=2)
    for suffix in ("a", "b", "c"):
        service._persist_new_snapshots(
            (_snapshot(f"{suffix}-old", closed),), host_id="mac-mini"
        )
    for suffix in ("d", "e"):
        service._persist_new_snapshots(
            (_snapshot(f"{suffix}-new", today),), host_id="mac-mini"
        )
    closed_dir = service.snapshot_dir / f"date={closed.date().isoformat()}"
    today_dir = service.snapshot_dir / f"date={today.date().isoformat()}"
    assert len(list(closed_dir.glob("*.parquet"))) == 3
    assert len(list(today_dir.glob("*.parquet"))) == 2

    result = compact_closed_snapshot_partitions(service.parquet_dir)

    assert len(list(closed_dir.glob("*.parquet"))) == 1, "closed day not compacted"
    assert len(list(today_dir.glob("*.parquet"))) == 2, "today must not be touched"
    assert result["partitions"] == 1
    assert result["files_before"] == 3
    assert result["files_after"] == 1


def test_compaction_preserves_every_row(tmp_path: Path) -> None:
    service = _service(tmp_path)
    closed = datetime.now(timezone.utc) - timedelta(days=3)
    for suffix in ("a", "b", "c"):
        service._persist_new_snapshots((_snapshot(suffix, closed),), host_id="mac-mini")

    def keys() -> set[str]:
        con = duckdb.connect(str(service.duckdb_path))
        try:
            return {
                row[0]
                for row in con.execute(
                    "SELECT dedup_key FROM provider_usage_snapshots"
                ).fetchall()
            }
        finally:
            con.close()

    before = keys()
    compact_closed_snapshot_partitions(service.parquet_dir)
    assert keys() == before == {"dedup-a", "dedup-b", "dedup-c"}


def test_legacy_flat_snapshots_are_folded_into_dated_partitions(
    tmp_path: Path,
) -> None:
    """Pre-#393 flat files are moved into day partitions on the next sweep.

    The writer once wrote one file per refresh directly under
    ``provider_usage_snapshots/``; 18,583 such flat files made the
    ``union_by_name=true`` view footer-scan every file at startup (#382). The
    maintenance pass must fold those legacy files into the same ``date=``
    partitions the writer now uses, so the scan count collapses to days.
    """
    from drover.server.parquet_io import atomic_write_table
    from drover.server.providers.types import provider_snapshot_table

    service = _service(tmp_path)
    when = datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc)
    legacy = service.snapshot_dir / "part-legacy-abc.parquet"
    atomic_write_table(
        provider_snapshot_table(_snapshot("legacy", when)),
        legacy,
        compression="zstd",
    )
    assert legacy.is_file()

    result = compact_closed_snapshot_partitions(service.parquet_dir)

    assert not legacy.exists(), "the flat legacy file must be folded away"
    partition = service.snapshot_dir / "date=2026-09-17"
    assert list(partition.glob("*.parquet")), "rows must land in a dated partition"

    con = duckdb.connect(str(service.duckdb_path))
    try:
        keys = {
            row[0]
            for row in con.execute(
                "SELECT dedup_key FROM provider_usage_snapshots"
            ).fetchall()
        }
    finally:
        con.close()
    assert keys == {"dedup-legacy"}
    assert result["files_before"] == 1
    assert result["rows"] == 1


def test_legacy_fold_retry_does_not_duplicate_a_partially_moved_file(
    tmp_path: Path, monkeypatch
) -> None:
    """A crash after the first replacement write must leave a retry safe."""
    from drover.server.parquet_io import atomic_write_table
    from drover.server.providers import service as service_module
    from drover.server.providers.types import provider_snapshot_table

    service = _service(tmp_path)
    first = datetime(2026, 9, 17, 23, 50, tzinfo=timezone.utc)
    second = first + timedelta(minutes=20)
    legacy = service.snapshot_dir / "part-legacy-retry.parquet"
    atomic_write_table(
        pa.concat_tables(
            [
                provider_snapshot_table(_snapshot("first", first)),
                provider_snapshot_table(_snapshot("second", second)),
            ]
        ),
        legacy,
        compression="zstd",
    )

    def write_then_interrupt(table, path, **kwargs):
        atomic_write_table(table, path, **kwargs)
        raise RuntimeError("interrupted after replacement write")

    monkeypatch.setattr(service_module, "atomic_write_table", write_then_interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        compact_closed_snapshot_partitions(service.parquet_dir)
    assert legacy.is_file()

    monkeypatch.setattr(service_module, "atomic_write_table", atomic_write_table)
    compact_closed_snapshot_partitions(service.parquet_dir)

    con = duckdb.connect(str(service.duckdb_path))
    try:
        rows = con.execute(
            "SELECT dedup_key, count(*) FROM provider_usage_snapshots "
            "WHERE dedup_key IN ('dedup-first', 'dedup-second') "
            "GROUP BY dedup_key ORDER BY dedup_key"
        ).fetchall()
    finally:
        con.close()
    assert rows == [("dedup-first", 1), ("dedup-second", 1)]


def test_the_watcher_sweep_compacts_closed_partitions(
    tmp_path: Path, monkeypatch
) -> None:
    """Wiring check: the maintenance pass runs it, so nobody has to remember."""
    from drover.server import watcher as watcher_module

    calls: list[Path] = []
    monkeypatch.setattr(
        watcher_module,
        "compact_closed_snapshot_partitions",
        lambda parquet_dir: calls.append(Path(parquet_dir)) or {},
    )
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    parquet = tmp_path / "parquet"
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet, duckdb_path=db)
    w = watcher_module.IncomingWatcher(
        incoming_dir=incoming,
        parquet_dir=parquet,
        duckdb_path=db,
        retention_days=7,
    )
    w.start()
    try:
        deadline = __import__("time").monotonic() + 10
        while not calls and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.1)
        assert calls == [parquet]
    finally:
        w.stop()
