"""Tests for the durable live-session recap worker."""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb

from drover.schema import bootstrap
from drover.server.db import control_plane_path
from drover.server.harness.recap_jobs import enqueue_live_recap
from drover.server.harness.recap_worker import LiveRecapWorker
from drover.server.summarizer.backends import BackendError


def recap_db(
    tmp_path: Path,
    *,
    session_id: str,
    recap: tuple[str, int] | None = None,
) -> tuple[Path, duckdb.DuckDBPyConnection]:
    """Create a bootstrapped database with one content-bearing event."""
    db = tmp_path / "recaps.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    con = duckdb.connect(str(control_plane_path(db)))
    con.execute(
        """INSERT INTO harness_events
           (event_id, session_id, event_type, content_preview, payload_json, seq)
           VALUES (?, ?, 'user_input', 'Fix the recap cards.', '{}', 8)""",
        [f"{session_id}-event-8", session_id],
    )
    if recap is not None:
        con.execute(
            """INSERT INTO live_session_recaps
               (session_id, recap_text, source_seq, generator_model, generated_at)
               VALUES (?, ?, ?, 'prior-model', now())""",
            [session_id, recap[0], recap[1]],
        )
    return db, con


def recap_row(db: Path, session_id: str) -> tuple[object, ...] | None:
    with duckdb.connect(str(control_plane_path(db))) as con:
        return con.execute(
            """SELECT recap_text, source_seq, generator_model
               FROM live_session_recaps WHERE session_id=?""",
            [session_id],
        ).fetchone()


def recap_job(db: Path, session_id: str) -> tuple[object, ...] | None:
    with duckdb.connect(str(control_plane_path(db))) as con:
        return con.execute(
            """SELECT desired_source_seq, status FROM live_recap_jobs
               WHERE session_id=?""",
            [session_id],
        ).fetchone()


def job_status(db: Path, session_id: str) -> str | None:
    row = recap_job(db, session_id)
    return str(row[1]) if row is not None else None


class StubBackend:
    name = "stub"
    model = "stub-recap-v1"

    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls = 0

    def summarize(self, prompt: str) -> dict:
        self.calls += 1
        return self.result


class FailingBackend:
    name = "failing"
    model = "failing-recap-v1"

    def __init__(self, message: str) -> None:
        self.message = message

    def summarize(self, prompt: str) -> dict:
        raise BackendError(self.message)


class BlockingBackend:
    name = "blocking"
    model = "blocking-recap-v1"

    def __init__(self) -> None:
        self._called = threading.Event()
        self._release = threading.Event()
        self._result: dict[str, object] | None = None

    def summarize(self, prompt: str) -> dict:
        self._called.set()
        assert self._release.wait(timeout=2)
        assert self._result is not None
        return self._result

    def wait_until_called(self) -> None:
        assert self._called.wait(timeout=2)

    def release(self, result: dict[str, object]) -> None:
        self._result = result
        self._release.set()


def test_worker_persists_normalized_recap_and_marks_matching_job_done(
    tmp_path: Path,
) -> None:
    db, con = recap_db(tmp_path, session_id="s1")
    enqueue_live_recap(con, "s1", 8)
    con.close()

    worker = LiveRecapWorker(
        duckdb_path=db,
        backend=StubBackend({"recap": "**Fix cards** and verify snapshots."}),
    )

    assert worker.drain_once() == 1
    assert recap_row(db, "s1")[:2] == ("Fix cards and verify snapshots.", 8)
    assert job_status(db, "s1") == "done"


def test_stale_worker_result_does_not_replace_newer_requested_recap(
    tmp_path: Path,
) -> None:
    db, con = recap_db(tmp_path, session_id="s1")
    enqueue_live_recap(con, "s1", 8)
    con.close()
    backend = BlockingBackend()
    thread = threading.Thread(
        target=LiveRecapWorker(duckdb_path=db, backend=backend).drain_once
    )
    thread.start()
    backend.wait_until_called()
    with duckdb.connect(str(control_plane_path(db))) as con:
        enqueue_live_recap(con, "s1", 10)
    backend.release({"recap": "Stale source eight recap."})
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert recap_row(db, "s1") is None
    assert recap_job(db, "s1") == (10, "pending")


def test_failed_refresh_keeps_previous_successful_recap(tmp_path: Path) -> None:
    db, con = recap_db(tmp_path, session_id="s1", recap=("Existing recap.", 8))
    enqueue_live_recap(con, "s1", 10)
    con.close()
    worker = LiveRecapWorker(duckdb_path=db, backend=FailingBackend("offline"))

    assert worker.drain_once() == 1
    assert recap_row(db, "s1")[:2] == ("Existing recap.", 8)
    assert job_status(db, "s1") == "retry_wait"


def test_stream_worker_acks_stale_source_sequence_without_generating(
    tmp_path: Path,
) -> None:
    """Redis duplicates cannot spend work on a superseded durable generation."""
    from drover.server.jobs import JobStream

    db, con = recap_db(tmp_path, session_id="s1")
    enqueue_live_recap(con, "s1", 10)
    con.execute("UPDATE live_recap_jobs SET stream_publish_needed=FALSE")
    con.close()
    stream = JobStream("live-recap")
    stream.add({"session_id": "s1", "source_seq": 8})
    backend = StubBackend({"recap": "This must not be generated."})

    assert (
        LiveRecapWorker(duckdb_path=db, backend=backend, job_stream=stream).drain_once()
        == 1
    )
    assert backend.calls == 0
    assert stream.pending() == []


def test_new_worker_recovers_an_expired_running_claim(tmp_path: Path) -> None:
    """A process crash cannot leave a live-recap generation running forever."""
    db, con = recap_db(tmp_path, session_id="s1")
    enqueue_live_recap(con, "s1", 8)
    con.execute("""UPDATE live_recap_jobs
           SET status='running', attempts=1,
               updated_at=now() - INTERVAL '6 minutes'
           WHERE session_id='s1'""")
    con.close()

    assert (
        LiveRecapWorker(
            duckdb_path=db, backend=StubBackend({"recap": "Recovered recap."})
        ).drain_once()
        == 1
    )
    assert recap_row(db, "s1")[:2] == ("Recovered recap.", 8)
    with duckdb.connect(str(control_plane_path(db))) as con:
        assert con.execute(
            "SELECT status, attempts FROM live_recap_jobs WHERE session_id='s1'"
        ).fetchone() == ("done", 2)


def test_stream_retry_acknowledges_delivery_and_retries_from_durable_due_time(
    tmp_path: Path,
) -> None:
    """A retry wait does not consume Redis redelivery budget before it is due."""
    from drover.server.jobs import JobStream

    db, con = recap_db(tmp_path, session_id="s1")
    enqueue_live_recap(con, "s1", 8)
    con.close()
    stream = JobStream("live-recap", max_deliveries=1)
    failing = LiveRecapWorker(
        duckdb_path=db, backend=FailingBackend("offline"), job_stream=stream
    )

    assert failing.drain_once() == 1
    assert stream.pending() == []
    assert stream.dead_letters() == []
    with duckdb.connect(str(control_plane_path(db))) as con:
        con.execute("""UPDATE live_recap_jobs
               SET next_run_at=now() - INTERVAL '1 second'
               WHERE session_id='s1'""")

    assert (
        LiveRecapWorker(
            duckdb_path=db,
            backend=StubBackend({"recap": "Retried from durable queue."}),
            job_stream=stream,
        ).drain_once()
        == 1
    )
    assert recap_row(db, "s1")[:2] == ("Retried from durable queue.", 8)
    assert job_status(db, "s1") == "done"
    assert stream.dead_letters() == []


def test_stream_redelivery_does_not_steal_an_unexpired_running_claim(
    tmp_path: Path,
) -> None:
    """A live worker retains its generation until the durable lease expires."""
    from drover.server.jobs import JobStream

    db, con = recap_db(tmp_path, session_id="s1")
    enqueue_live_recap(con, "s1", 8)
    con.execute("""UPDATE live_recap_jobs
           SET status='running', attempts=1, stream_publish_needed=FALSE,
               updated_at=now()
           WHERE session_id='s1'""")
    con.close()
    stream = JobStream("live-recap", visibility_timeout_ms=0)
    stream.add({"session_id": "s1", "source_seq": 8})
    assert stream.read_group("original", count=1)
    backend = StubBackend({"recap": "A second worker must not run."})

    assert (
        LiveRecapWorker(duckdb_path=db, backend=backend, job_stream=stream).drain_once()
        == 0
    )
    assert backend.calls == 0
    with duckdb.connect(str(control_plane_path(db))) as con:
        assert con.execute(
            "SELECT status, attempts FROM live_recap_jobs WHERE session_id='s1'"
        ).fetchone() == ("running", 1)
