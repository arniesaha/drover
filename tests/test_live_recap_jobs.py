"""Live recap enqueue/read tests: recaps are the live phase of session memory (#480)."""

from __future__ import annotations

from pathlib import Path

import duckdb

from drover.schema import bootstrap
from drover.server.db import control_plane_connection, control_plane_path
from drover.server.harness.recap_jobs import (
    LiveRecap,
    enqueue_live_recap,
    latest_live_recaps,
)
from drover.server.ledger import RECAP_SESSION, transaction
from drover.server.memory_store import LiveRecap as MemoryLiveRecap
from drover.server.memory_store import MemoryRepository


def _enqueue(path: Path, session_id: str, seq: int) -> bool:
    with control_plane_connection(path) as con:
        with transaction(con):
            return enqueue_live_recap(con, session_id, seq, store_path=path)


def _live_jobs(path: Path, session_id: str) -> list[tuple]:
    with control_plane_connection(path) as con:
        return con.execute(
            """SELECT source_version, payload_json, status FROM pipeline_jobs
                WHERE job_kind = ? AND subject_key = ?
                  AND status IN ('pending', 'running', 'retry_wait')""",
            [RECAP_SESSION, session_id],
        ).fetchall()


def test_live_recap_is_re_exported_from_memory_store() -> None:
    """Callers such as metrics.harness_snapshot import it from recap_jobs."""
    assert LiveRecap is MemoryLiveRecap


def test_enqueue_only_advances_a_session_forward(pg_control_path: Path) -> None:
    """An older or replayed completion never retargets a newer waiting job."""
    assert _enqueue(pg_control_path, "s1", 10) is True
    assert _enqueue(pg_control_path, "s1", 9) is False
    assert _enqueue(pg_control_path, "s1", 10) is False
    assert _live_jobs(pg_control_path, "s1") == [
        ("10", '{"source_seq": 10}', "pending")
    ]

    assert _enqueue(pg_control_path, "s1", 12) is True
    assert _live_jobs(pg_control_path, "s1") == [
        ("12", '{"source_seq": 12}', "pending")
    ]


def test_enqueue_skips_a_sequence_the_stored_recap_already_covers(
    pg_control_path: Path,
) -> None:
    """With no live job left, the stored recap is what fences replays."""
    with control_plane_connection(pg_control_path) as con:
        with transaction(con):
            MemoryRepository.put_recap(con, "s1", "Covered.", 15, "m")

    assert _enqueue(pg_control_path, "s1", 12) is False
    assert _enqueue(pg_control_path, "s1", 15) is False
    assert _live_jobs(pg_control_path, "s1") == []
    assert _enqueue(pg_control_path, "s1", 16) is True


def test_enqueue_rolls_back_with_the_callers_transaction(pg_control_path: Path) -> None:
    """The enqueue joins the caller's transaction; an aborted append leaves no job."""
    with control_plane_connection(pg_control_path) as con:
        con.execute("BEGIN")
        assert enqueue_live_recap(con, "s1", 10, store_path=pg_control_path) is True
        con.execute("ROLLBACK")

    assert _live_jobs(pg_control_path, "s1") == []


def test_latest_live_recaps_returns_typed_requested_projection(
    pg_control_path: Path,
) -> None:
    """Consumers receive only requested sessions as typed recap records."""
    with control_plane_connection(pg_control_path) as con:
        with transaction(con):
            MemoryRepository.put_recap(con, "s1", "first recap", 7, "recap-model")
            MemoryRepository.put_recap(con, "s2", "other recap", 8, None)

    recaps = latest_live_recaps(pg_control_path, ["s1", "missing"])

    assert recaps.keys() == {"s1"}
    recap = recaps["s1"]
    assert isinstance(recap, LiveRecap)
    assert (recap.session_id, recap.text, recap.source_seq, recap.generator_model) == (
        "s1",
        "first recap",
        7,
        "recap-model",
    )
    assert recap.generated_at is not None


def test_duckdb_control_plane_has_no_live_recaps(tmp_path: Path) -> None:
    """Without PostgreSQL, memory is unavailable: no-op enqueue, empty reads."""
    duckdb_path = tmp_path / "recaps.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    with duckdb.connect(str(control_plane_path(duckdb_path))) as con:
        assert enqueue_live_recap(con, "s1", 10, store_path=duckdb_path) is False

    assert latest_live_recaps(duckdb_path, ["s1"]) == {}
    assert latest_live_recaps(duckdb_path, []) == {}
