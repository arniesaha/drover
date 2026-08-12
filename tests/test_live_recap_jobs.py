"""Durable, coalescing queue tests for live session recaps."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb

from drover.schema import bootstrap
from drover.server.db import control_plane_path
from drover.server.harness.recap_jobs import (
    LiveRecap,
    enqueue_live_recap,
    flush_live_recap_publications,
    latest_live_recaps,
    publish_live_recap_generation,
)


def _bootstrapped(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    duckdb_path = tmp_path / "recaps.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    # The recap queue lives in the control-plane store since #95: the registry
    # enqueues in the same transaction as the event that triggered it.
    return duckdb.connect(str(control_plane_path(duckdb_path)))


def test_enqueue_coalesces_to_newest_source_seq(tmp_path: Path) -> None:
    """Older events cannot reset a newer pending recap generation."""
    con = _bootstrapped(tmp_path)
    try:
        assert enqueue_live_recap(con, "s1", 10) is True
        assert enqueue_live_recap(con, "s1", 9) is False
        assert enqueue_live_recap(con, "s1", 12) is True
        row = con.execute(
            "SELECT desired_source_seq, status, attempts "
            "FROM live_recap_jobs WHERE session_id='s1'"
        ).fetchone()
        assert row == (12, "pending", 0)
    finally:
        con.close()


class _RecordingStream:
    def __init__(self) -> None:
        self.messages: list[dict[str, int | str]] = []

    def add(self, fields: dict[str, int | str]) -> None:
        self.messages.append(fields)


def test_publish_live_recap_generation_clears_durable_pending_flag(
    tmp_path: Path,
) -> None:
    """A matching generation publishes once and records that durable fact."""
    con = _bootstrapped(tmp_path)
    stream = _RecordingStream()
    try:
        assert enqueue_live_recap(con, "s1", 10) is True

        assert publish_live_recap_generation(con, "s1", 9, stream) is False
        assert publish_live_recap_generation(con, "s1", 10, stream) is True
        assert stream.messages == [{"session_id": "s1", "source_seq": 10}]
        assert con.execute(
            "SELECT stream_publish_needed FROM live_recap_jobs WHERE session_id='s1'"
        ).fetchone() == (False,)
    finally:
        con.close()


def test_flush_live_recap_publications_retries_pending_publications(
    tmp_path: Path,
) -> None:
    """The poll path can recover publications left pending by a prior crash."""
    con = _bootstrapped(tmp_path)
    stream = _RecordingStream()
    try:
        assert enqueue_live_recap(con, "s1", 10) is True
        assert enqueue_live_recap(con, "s2", 20) is True

        assert flush_live_recap_publications(con, stream, limit=1) == 1
        assert stream.messages == [{"session_id": "s1", "source_seq": 10}]
        assert con.execute(
            "SELECT count(*) FROM live_recap_jobs WHERE stream_publish_needed"
        ).fetchone() == (1,)

        assert flush_live_recap_publications(con, stream) == 1
        assert stream.messages == [
            {"session_id": "s1", "source_seq": 10},
            {"session_id": "s2", "source_seq": 20},
        ]
    finally:
        con.close()


def test_latest_live_recaps_returns_typed_requested_projection(tmp_path: Path) -> None:
    """Consumers receive only requested sessions as typed recap records."""
    con = _bootstrapped(tmp_path)
    generated_at = datetime(2026, 8, 11, 12, 30)
    try:
        con.execute(
            "INSERT INTO live_session_recaps "
            "(session_id, recap_text, source_seq, generator_model, generated_at) "
            "VALUES (?, ?, ?, ?, ?), (?, ?, ?, ?, ?)",
            [
                "s1",
                "first recap",
                7,
                "recap-model",
                generated_at,
                "s2",
                "other recap",
                8,
                None,
                generated_at,
            ],
        )

        assert latest_live_recaps(con, ["s1", "missing"]) == {
            "s1": LiveRecap(
                session_id="s1",
                text="first recap",
                source_seq=7,
                generated_at=generated_at,
                generator_model="recap-model",
            )
        }
    finally:
        con.close()
