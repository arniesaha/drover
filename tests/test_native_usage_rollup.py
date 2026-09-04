"""Bounded native-history usage rollup tests."""

from __future__ import annotations

import json
import time

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.db import control_plane_path
from drover.server.harness.usage_sources import SOURCE_NATIVE_AGENT_EVENTS
from drover.server.ingest import ingest_file
from drover.server.native_usage_rollup import (
    NativeUsageRollupReport,
    NativeUsageRollupWorker,
    rollup_pending_native_usage,
)


def _ingest_rows(tmp_path, parquet_dir, duckdb_path, rows: list[dict]) -> None:
    incoming = tmp_path / "native_usage.jsonl"
    incoming.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    assert ingest_file(
        incoming, parquet_dir=parquet_dir, duckdb_path=duckdb_path
    ).inserted == len(rows)


def test_native_usage_rollup_materializes_typed_usage_by_session(tmp_path):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _ingest_rows(
        tmp_path,
        parquet_dir,
        duckdb_path,
        [
            {
                "id": "native-usage-1",
                "session_id": "native-session",
                "timestamp": "2026-09-04T12:00:00Z",
                "agent_id": "claude-code",
                "event_type": "assistant_message",
                "message": {"role": "assistant", "content": "first"},
                "token_usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 80,
                },
                "raw_data": {},
            },
            {
                "id": "native-usage-2",
                "session_id": "native-session",
                "timestamp": "2026-09-04T12:01:00Z",
                "agent_id": "claude-code",
                "event_type": "assistant_message",
                "message": {"role": "assistant", "content": "second"},
                "token_usage": {
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "cache_creation_input_tokens": 5,
                },
                "raw_data": {},
            },
        ],
    )

    report = rollup_pending_native_usage(duckdb_path)

    assert (report.partitions, report.sessions) == (1, 1)
    with duckdb.connect(str(control_plane_path(duckdb_path))) as con:
        assert (
            con.execute("""
            SELECT input_tokens, output_tokens, cache_read_tokens,
                   cache_write_tokens, turn_count, source, source_seq,
                   source_event_count, host_id, harness
            FROM session_usage
            WHERE session_id = 'native-session'
            """).fetchone()
            == (
                150,
                30,
                80,
                5,
                2,
                SOURCE_NATIVE_AGENT_EVENTS,
                2,
                2,
                None,
                None,
            )
        )
    assert rollup_pending_native_usage(duckdb_path).partitions == 0


def test_native_usage_rollup_aggregates_one_session_across_changed_dates(tmp_path):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _ingest_rows(
        tmp_path,
        parquet_dir,
        duckdb_path,
        [
            {
                "id": "native-day-one",
                "session_id": "cross-date-session",
                "timestamp": "2026-09-03T23:59:00Z",
                "agent_id": "claude-code",
                "event_type": "assistant_message",
                "message": {"role": "assistant", "content": "first day"},
                "token_usage": {"input_tokens": 100},
                "raw_data": {},
            },
            {
                "id": "native-day-two",
                "session_id": "cross-date-session",
                "timestamp": "2026-09-04T00:01:00Z",
                "agent_id": "claude-code",
                "event_type": "assistant_message",
                "message": {"role": "assistant", "content": "second day"},
                "token_usage": {"output_tokens": 25, "cache_read_input_tokens": 7},
                "raw_data": {},
            },
        ],
    )

    report = rollup_pending_native_usage(duckdb_path, limit=2)

    assert (report.partitions, report.sessions) == (2, 2)
    with duckdb.connect(str(control_plane_path(duckdb_path))) as con:
        assert con.execute("""
            SELECT input_tokens, output_tokens, cache_read_tokens, turn_count,
                   source_seq, source_event_count
            FROM session_usage
            WHERE session_id = 'cross-date-session'
            """).fetchone() == (100, 25, 7, 2, 2, 2)


def test_native_usage_rollup_marks_unobserved_partitions_without_zero_usage(tmp_path):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _ingest_rows(
        tmp_path,
        parquet_dir,
        duckdb_path,
        [
            {
                "id": "native-without-usage",
                "session_id": "unobserved-native-session",
                "timestamp": "2026-09-04T12:00:00Z",
                "agent_id": "claude-code",
                "event_type": "assistant_message",
                "message": {"role": "assistant", "content": "no totals"},
                "raw_data": {},
            }
        ],
    )

    report = rollup_pending_native_usage(duckdb_path)

    assert (report.partitions, report.sessions) == (1, 0)
    with duckdb.connect(str(control_plane_path(duckdb_path))) as con:
        assert (
            con.execute(
                "SELECT * FROM session_usage WHERE session_id = 'unobserved-native-session'"
            ).fetchone()
            is None
        )
    assert rollup_pending_native_usage(duckdb_path).partitions == 0


def test_native_usage_rollup_worker_survives_a_failed_pass(tmp_path, monkeypatch):
    from drover.server import native_usage_rollup

    calls = []

    def explode(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return NativeUsageRollupReport(partitions=0, sessions=0)

    monkeypatch.setattr(native_usage_rollup, "rollup_pending_native_usage", explode)
    worker = NativeUsageRollupWorker(
        duckdb_path=tmp_path / "drover.duckdb", poll_interval_s=0.01
    )
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()
    assert len(calls) >= 2


def test_native_usage_rollup_does_not_advance_watermark_after_a_write_failure(
    tmp_path, monkeypatch
):
    from drover.server import native_usage_rollup

    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _ingest_rows(
        tmp_path,
        parquet_dir,
        duckdb_path,
        [
            {
                "id": "native-write-failure",
                "session_id": "retry-native-session",
                "timestamp": "2026-09-04T12:00:00Z",
                "agent_id": "claude-code",
                "event_type": "assistant_message",
                "message": {"role": "assistant", "content": "retry me"},
                "token_usage": {"input_tokens": 10},
                "raw_data": {},
            }
        ],
    )

    def fail_source_write(*_args, **_kwargs) -> None:
        raise RuntimeError("source write failed")

    monkeypatch.setattr(native_usage_rollup, "upsert_source_usage", fail_source_write)
    with pytest.raises(RuntimeError, match="source write failed"):
        rollup_pending_native_usage(duckdb_path)

    with duckdb.connect(str(control_plane_path(duckdb_path))) as con:
        assert (
            con.execute("SELECT * FROM native_usage_partition_watermarks").fetchall()
            == []
        )
        assert (
            con.execute("SELECT * FROM native_usage_partition_totals").fetchall() == []
        )
