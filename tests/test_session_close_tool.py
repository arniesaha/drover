"""Tests for drover_session_close enqueue semantics."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.mcp.tools import drover_session_close


def _seed(tmp_path: Path) -> Path:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return duckdb_path


def _job_row(duckdb_path: Path, session_id: str) -> tuple | None:
    con = duckdb.connect(str(duckdb_path))
    try:
        return con.execute(
            "SELECT session_id, status, attempts, last_error FROM summarize_jobs WHERE session_id=?",
            [session_id],
        ).fetchone()
    finally:
        con.close()


def test_close_inserts_pending_job(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    out = drover_session_close(duckdb_path=db, session_id="s1")
    assert out == {"session_id": "s1", "status": "queued"}

    row = _job_row(db, "s1")
    assert row[0] == "s1" and row[1] == "pending"


def test_close_is_idempotent_when_pending(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    drover_session_close(duckdb_path=db, session_id="s1")
    out = drover_session_close(duckdb_path=db, session_id="s1")
    assert out["status"] == "already_queued"


def test_close_no_op_when_done(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    drover_session_close(duckdb_path=db, session_id="s1")
    con = duckdb.connect(str(db))
    try:
        con.execute("UPDATE summarize_jobs SET status='done' WHERE session_id='s1'")
    finally:
        con.close()
    out = drover_session_close(duckdb_path=db, session_id="s1")
    assert out["status"] == "already_done"


def test_close_does_not_requeue_same_dead_lettered_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _seed(tmp_path)
    monkeypatch.setattr(
        "drover.server.mcp.tools.source_version_for_session", lambda con, sid: "v1"
    )
    published: list[dict] = []

    class FakeStream:
        def add(self, fields: dict) -> str:
            published.append(fields)
            return "1-0"

    stream = FakeStream()
    drover_session_close(duckdb_path=db, session_id="s1", summarize_job_stream=stream)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "UPDATE summarize_jobs SET status='dead_lettered', attempts=5 "
            "WHERE session_id='s1'"
        )
    finally:
        con.close()
    out = drover_session_close(
        duckdb_path=db, session_id="s1", summarize_job_stream=stream
    )
    assert out["status"] == "dead_lettered"

    row = _job_row(db, "s1")
    assert row[1] == "dead_lettered"
    assert row[2] == 5
    assert published == [{"session_id": "s1", "source_version": "v1"}]


def test_close_publishes_only_changed_source_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _seed(tmp_path)
    version = {"value": "v1"}
    monkeypatch.setattr(
        "drover.server.mcp.tools.source_version_for_session",
        lambda con, sid: version["value"],
    )
    published: list[dict] = []

    class FakeStream:
        def add(self, fields: dict) -> str:
            published.append(fields)
            return "1-0"

    stream = FakeStream()
    assert (
        drover_session_close(
            duckdb_path=db, session_id="s1", summarize_job_stream=stream
        )["status"]
        == "queued"
    )
    assert (
        drover_session_close(
            duckdb_path=db, session_id="s1", summarize_job_stream=stream
        )["status"]
        == "already_queued"
    )
    version["value"] = "v2"
    assert (
        drover_session_close(
            duckdb_path=db, session_id="s1", summarize_job_stream=stream
        )["status"]
        == "requeued"
    )

    assert published == [
        {"session_id": "s1", "source_version": "v1"},
        {"session_id": "s1", "source_version": "v2"},
    ]
