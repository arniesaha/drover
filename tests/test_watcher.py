"""Tests for src/drover/server/watcher.py."""

import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.db import control_plane_connection, control_plane_path
from drover.server.summarizer.jobs import source_version_for_session
from drover.server.watcher import (
    IncomingWatcher,
    _Handler,
    sweep_advisory_occurrences,
    sweep_receipts,
)


@pytest.fixture
def lh(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    parquet_dir = tmp_path / "parquet"
    db_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    return incoming, parquet_dir, db_path


def _write_event(jsonl_path: Path, event_id: str) -> None:
    line = json.dumps(
        {
            "id": event_id,
            "session_id": "sess-x",
            "timestamp": "2026-05-08T10:00:00Z",
            "agent_id": "test-agent",
            "event_type": "user_message",
            "message": {"role": "user", "content": "hi"},
            "raw_data": {
                "_repo_owner": "arniesaha",
                "_repo_name": "nexus",
                "gitBranch": "main",
            },
        }
    )
    jsonl_path.write_text(line + "\n")


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_start_ingests_the_backlog_before_it_returns(lh):
    """The backlog runs on the caller's thread, as it did through 0.4.15.

    0.4.16 moved it onto a thread so the port could bind sooner. In production
    that put the backlog, the retention sweeps and every worker on a slow disk
    at the moment the fleet started polling, and `/harness` stayed at "fleet
    listing busy" for eight minutes until the release was rolled back.
    """
    incoming, parquet_dir, db_path = lh
    host_dir = incoming / "macmini"
    host_dir.mkdir()
    _write_event(host_dir / "backlog-001.jsonl", "backlog-001")
    w = IncomingWatcher(
        incoming_dir=incoming, parquet_dir=parquet_dir, duckdb_path=db_path
    )
    w.start()
    try:
        assert w.backlog_done.is_set(), "start() returned before the backlog"
        assert not (host_dir / "backlog-001.jsonl").exists()
        assert (host_dir / ".processed" / "backlog-001.jsonl").exists()
    finally:
        w.stop()


def test_retention_sweeps_start_only_after_the_backlog(lh):
    incoming, parquet_dir, db_path = lh
    order: list[str] = []
    w = IncomingWatcher(
        incoming_dir=incoming,
        parquet_dir=parquet_dir,
        duckdb_path=db_path,
        retention_days=7,
    )
    with (
        mock.patch.object(
            w, "_ingest_backlog", side_effect=lambda: order.append("backlog")
        ),
        mock.patch.object(
            w, "_start_sweeper", side_effect=lambda: order.append("sweeper")
        ),
    ):
        w.start()
        try:
            assert order == ["backlog", "sweeper"]
        finally:
            w.stop()


def test_backlog_failure_still_marks_backlog_done(lh):
    incoming, parquet_dir, db_path = lh
    (incoming / "macmini").mkdir()
    _write_event(incoming / "macmini" / "backlog-002.jsonl", "backlog-002")
    w = IncomingWatcher(
        incoming_dir=incoming, parquet_dir=parquet_dir, duckdb_path=db_path
    )
    with mock.patch.object(
        w._handler, "_maybe_ingest", side_effect=RuntimeError("boom")
    ):
        w.start()
        try:
            assert w.backlog_done.wait(timeout=10)
        finally:
            w.stop()


def test_backlog_count_ignores_the_processed_archive(lh, caplog):
    """`rglob` also walks `.processed/`, which `_maybe_ingest` skips; those
    audit copies must not be counted as backlog the pass attempted.
    """
    incoming, parquet_dir, db_path = lh
    host = incoming / "macmini"
    (host / ".processed").mkdir(parents=True)
    _write_event(host / "backlog-004.jsonl", "backlog-004")
    _write_event(host / ".processed" / "done-001.jsonl", "done-001")
    _write_event(host / ".processed" / "done-002.jsonl", "done-002")
    w = IncomingWatcher(
        incoming_dir=incoming, parquet_dir=parquet_dir, duckdb_path=db_path
    )
    caplog.set_level(logging.INFO, logger="drover.watcher")
    w.start()
    try:
        assert w.backlog_done.wait(timeout=10)
        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "drover.watcher"
            and record.getMessage().startswith("watcher backlog:")
        ]
        assert len(messages) == 1
        assert messages[0].startswith("watcher backlog: 1 file(s)"), messages
    finally:
        w.stop()


def test_backlog_logs_when_the_pass_finishes(lh, caplog):
    """Nothing else tells an operator when the startup backlog pass finished
    since it stopped blocking `start()`; the `finally` block in
    `_ingest_backlog` must log a count and duration at INFO.
    """
    incoming, parquet_dir, db_path = lh
    (incoming / "macmini").mkdir()
    _write_event(incoming / "macmini" / "backlog-003.jsonl", "backlog-003")
    w = IncomingWatcher(
        incoming_dir=incoming, parquet_dir=parquet_dir, duckdb_path=db_path
    )
    caplog.set_level(logging.INFO, logger="drover.watcher")
    w.start()
    try:
        assert w.backlog_done.wait(timeout=10)
        assert any(
            record.name == "drover.watcher"
            and record.levelno == logging.INFO
            and "watcher backlog: 1 file(s)" in record.getMessage()
            for record in caplog.records
        )
    finally:
        w.stop()


def test_maybe_ingest_rechecks_file_existence_under_the_lock(lh, caplog):
    """A file moved by a concurrent caller while this one waited on the lock
    must not be logged as an ingest failure.

    `_maybe_ingest`'s outer `path.is_file()` check runs before `self._lock`
    is acquired, so two callers -- the live observer and the startup backlog
    pass, say -- can both pass it for the same file. Without a re-check once
    the lock is held, the loser calls `_ingest_once` on a file the winner
    already moved to `.processed/`; `ingest_file` then raises
    `FileNotFoundError`, which is not DuckDB lock contention, so it is logged
    as `log.exception("ingest failed for %s; leaving file in place", path)` --
    a false ERROR for what is actually a benign, already-handled outcome.
    """
    incoming, parquet_dir, db_path = lh
    host_dir = incoming / "macmini"
    host_dir.mkdir()
    path = host_dir / "race.jsonl"
    _write_event(path, "race-001")

    handler = _Handler(parquet_dir, db_path)
    caplog.set_level(logging.ERROR)

    handler._lock.acquire()
    try:
        thread = threading.Thread(target=handler._maybe_ingest, args=(path,))
        thread.start()
        # There is no event to wait on for "another thread is now blocked
        # acquiring a lock", so a bounded join is the most deterministic
        # signal available short of instrumenting the lock itself: it gives
        # the thread time to clear the outer is_file() check and reach
        # self._lock, and it fails loudly via the assertion below -- rather
        # than racing ahead silently -- if that did not happen within the
        # timeout. This is polling a fixed condition to a hard deadline, not
        # a bare sleep-as-synchronisation.
        thread.join(timeout=1)
        assert thread.is_alive(), "thread did not block on the lock as expected"

        processed = host_dir / ".processed"
        processed.mkdir(exist_ok=True)
        target = processed / path.name
        shutil.move(str(path), str(target))
    finally:
        handler._lock.release()

    thread.join(timeout=10)
    assert not thread.is_alive(), "_maybe_ingest never returned"
    assert target.exists(), "the winner's move must be left untouched"
    assert not path.exists()
    assert not any(
        record.levelno >= logging.ERROR for record in caplog.records
    ), "a file moved by another caller must not be logged as a failure"


def test_watcher_picks_up_dropped_file(lh):
    incoming, parquet_dir, db_path = lh
    host_dir = incoming / "macmini"
    host_dir.mkdir()

    w = IncomingWatcher(
        incoming_dir=incoming, parquet_dir=parquet_dir, duckdb_path=db_path
    )
    w.start()
    try:
        target = host_dir / "batch-001.jsonl"
        # Atomic-rename pattern: write to .tmp, then rename
        tmp = target.with_suffix(".jsonl.tmp")
        _write_event(tmp, "watcher-001")
        tmp.rename(target)

        def has_row():
            con = duckdb.connect(str(db_path))
            try:
                return (
                    con.execute(
                        "SELECT count(*) FROM agent_events WHERE id = 'watcher-001'"
                    ).fetchone()[0]
                    == 1
                )
            finally:
                con.close()

        assert _wait_for(has_row), "row never landed in agent_events"
    finally:
        w.stop()


def test_watcher_moves_file_to_processed(lh):
    incoming, parquet_dir, db_path = lh
    host_dir = incoming / "macmini"
    host_dir.mkdir()

    w = IncomingWatcher(
        incoming_dir=incoming, parquet_dir=parquet_dir, duckdb_path=db_path
    )
    w.start()
    try:
        target = host_dir / "batch-002.jsonl"
        tmp = target.with_suffix(".jsonl.tmp")
        _write_event(tmp, "watcher-002")
        tmp.rename(target)

        def is_moved():
            return (host_dir / ".processed" / "batch-002.jsonl").exists()

        assert _wait_for(is_moved), "file never moved to .processed/"
        assert not target.exists(), "original file should have been removed"
    finally:
        w.stop()


def test_watcher_ignores_tmp_files(lh):
    incoming, parquet_dir, db_path = lh
    host_dir = incoming / "macmini"
    host_dir.mkdir()

    w = IncomingWatcher(
        incoming_dir=incoming, parquet_dir=parquet_dir, duckdb_path=db_path
    )
    w.start()
    try:
        tmp = host_dir / "batch-003.jsonl.tmp"
        _write_event(tmp, "watcher-003")
        time.sleep(0.5)  # give the watcher a chance to (incorrectly) act

        con = duckdb.connect(str(db_path))
        try:
            n = con.execute(
                "SELECT count(*) FROM agent_events WHERE id = 'watcher-003'"
            ).fetchone()[0]
        finally:
            con.close()
        assert n == 0, "watcher should not process .tmp files"
        assert tmp.exists(), ".tmp file should still be in place"
    finally:
        w.stop()


def _write_two_session_events(jsonl_path: Path) -> None:
    """Write a JSONL file containing events for two distinct session_ids."""
    lines = [
        json.dumps(
            {
                "id": "watcher-s1-001",
                "session_id": "sess-w1",
                "timestamp": "2026-05-08T11:00:00Z",
                "agent_id": "test-agent",
                "event_type": "user_message",
                "message": {"role": "user", "content": "hello session 1"},
                "raw_data": {
                    "_repo_owner": "arniesaha",
                    "_repo_name": "nexus",
                    "gitBranch": "main",
                },
            }
        ),
        json.dumps(
            {
                "id": "watcher-s2-001",
                "session_id": "sess-w2",
                "timestamp": "2026-05-08T11:00:05Z",
                "agent_id": "test-agent",
                "event_type": "user_message",
                "message": {"role": "user", "content": "hello session 2"},
                "raw_data": {
                    "_repo_owner": "arniesaha",
                    "_repo_name": "nexus",
                    "gitBranch": "main",
                },
            }
        ),
    ]
    jsonl_path.write_text("\n".join(lines) + "\n")


@pytest.fixture
def pg_lh(tmp_path, pg_control_path):
    """Like ``lh``, with the PostgreSQL control store (and so the job ledger)."""
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    parquet_dir = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=pg_control_path)
    return incoming, parquet_dir, pg_control_path


def _summary_jobs(store_path: Path) -> list[tuple]:
    with control_plane_connection(store_path) as con:
        return con.execute(
            """SELECT subject_key, status, source_version FROM pipeline_jobs
                WHERE job_kind = 'summarize_session'
                ORDER BY subject_key, enqueued_at"""
        ).fetchall()


def _source_version(db_path: Path, session_id: str) -> str:
    con = duckdb.connect(str(db_path))
    try:
        return source_version_for_session(con, session_id)
    finally:
        con.close()


def test_watcher_enqueues_summarize_jobs(pg_lh):
    """After ingesting a JSONL with 2 distinct sessions, both get a pending
    summarize_session ledger job. Re-ingesting the same file must not
    duplicate them."""
    incoming, parquet_dir, db_path = pg_lh
    host_dir = incoming / "macmini"
    host_dir.mkdir()

    w = IncomingWatcher(
        incoming_dir=incoming, parquet_dir=parquet_dir, duckdb_path=db_path
    )
    w.start()
    try:
        target = host_dir / "batch-multi.jsonl"
        tmp = target.with_suffix(".jsonl.tmp")
        _write_two_session_events(tmp)
        tmp.rename(target)

        assert _wait_for(
            lambda: len(_summary_jobs(db_path)) == 2
        ), "summarize jobs never enqueued for new sessions"
        versions = {
            sid: _source_version(db_path, sid) for sid in ("sess-w1", "sess-w2")
        }
        assert _summary_jobs(db_path) == [
            ("sess-w1", "pending", versions["sess-w1"]),
            ("sess-w2", "pending", versions["sess-w2"]),
        ]

        # Re-ingest: write same events under a different filename and drop it
        target2 = host_dir / "batch-multi-dup.jsonl"
        tmp2 = target2.with_suffix(".jsonl.tmp")
        _write_two_session_events(tmp2)
        tmp2.rename(target2)

        # Give the watcher time to process the duplicate file
        def dup_processed():
            return (host_dir / ".processed" / "batch-multi-dup.jsonl").exists()

        assert _wait_for(dup_processed), "duplicate batch file never processed"

        # Same source generations: the live jobs are reused, not duplicated.
        assert len(_summary_jobs(db_path)) == 2
    finally:
        w.stop()


def test_ingest_without_postgres_skips_summary_enqueue(
    lh, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DuckDB control store has no ledger: ingest proceeds, nothing is enqueued."""
    incoming, parquet_dir, db_path = lh
    host_dir = incoming / "macmini"
    host_dir.mkdir()
    batch = host_dir / "batch.jsonl"
    _write_two_session_events(batch)

    def never(*_args, **_kwargs):
        raise AssertionError("no summary work without the PostgreSQL ledger")

    monkeypatch.setattr("drover.server.watcher.source_version_for_session", never)
    monkeypatch.setattr("drover.server.watcher.enqueue_summary_generation", never)
    _Handler(parquet_dir, db_path, max_lock_retries=0)._maybe_ingest(batch)

    assert (host_dir / ".processed" / "batch.jsonl").exists()
    con = duckdb.connect(str(db_path))
    try:
        assert con.execute(
            "SELECT count(*) FROM agent_events WHERE session_id IN ('sess-w1', 'sess-w2')"
        ).fetchone() == (2,)
    finally:
        con.close()


def test_handler_retries_duckdb_lock_and_moves_only_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    incoming = tmp_path / "incoming"
    host_dir = incoming / "nas-claude"
    host_dir.mkdir(parents=True)
    batch = host_dir / "openclaw.jsonl"
    batch.write_text("{}\n")
    calls = {"count": 0}

    class Stats:
        read = 1
        inserted = 1
        skipped_dupes = 0
        errors = 0
        shadow_published = 0
        new_session_ids = []

    def fake_ingest_file(
        path: Path, *, parquet_dir: Path, duckdb_path: Path, shadow_publisher=None
    ):
        calls["count"] += 1
        assert path == batch
        if calls["count"] == 1:
            raise duckdb.IOException(
                "Could not set lock on file drover.duckdb: Conflicting lock is held"
            )
        assert batch.exists(), "retry must not move/remove the source before success"
        return Stats()

    monkeypatch.setattr("drover.server.watcher.ingest_file", fake_ingest_file)
    monkeypatch.setattr("drover.server.watcher.time.sleep", lambda _seconds: None)
    handler = _Handler(
        parquet_dir=tmp_path / "parquet",
        duckdb_path=tmp_path / "drover.duckdb",
        max_lock_retries=1,
        lock_retry_base_seconds=0,
    )

    handler._maybe_ingest(batch)

    assert calls["count"] == 2
    assert not batch.exists()
    assert (host_dir / ".processed" / "openclaw.jsonl").exists()
    assert "DuckDB lock contention" in caplog.text


def test_handler_recovers_summarize_enqueue_after_post_ingest_lock(
    tmp_path: Path, pg_control_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    host_dir = incoming / "nas-claude"
    host_dir.mkdir(parents=True)
    batch = host_dir / "openclaw.jsonl"
    batch.write_text(
        json.dumps(
            {
                "id": "event-after-ingest-lock",
                "session_id": "sess-after-ingest-lock",
                "timestamp": "2026-05-08T10:00:00Z",
                "agent_id": "test-agent",
                "event_type": "user_message",
                "message": {"role": "user", "content": "hi"},
            }
        )
        + "\n"
    )
    parquet_dir = tmp_path / "parquet"
    db_path = pg_control_path
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    calls = {"ingest": 0, "connect": 0}
    real_connect = duckdb.connect

    class Stats:
        read = 1
        inserted = 1
        skipped_dupes = 0
        errors = 0
        shadow_published = 0

        def __init__(self, new_session_ids):
            self.new_session_ids = new_session_ids

    def fake_ingest_file(
        path: Path, *, parquet_dir: Path, duckdb_path: Path, shadow_publisher=None
    ):
        calls["ingest"] += 1
        if calls["ingest"] == 1:
            return Stats({"sess-after-ingest-lock"})
        return Stats(set())

    def flaky_connect(*args, **kwargs):
        calls["connect"] += 1
        if calls["connect"] == 1:
            raise duckdb.IOException(
                "Could not set lock on file drover.duckdb: Conflicting lock is held"
            )
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("drover.server.watcher.ingest_file", fake_ingest_file)
    monkeypatch.setattr("drover.server.watcher.duckdb.connect", flaky_connect)
    monkeypatch.setattr("drover.server.watcher.time.sleep", lambda _seconds: None)
    handler = _Handler(
        parquet_dir=parquet_dir,
        duckdb_path=db_path,
        max_lock_retries=1,
        lock_retry_base_seconds=0,
    )

    handler._maybe_ingest(batch)

    assert calls["ingest"] == 2
    assert not batch.exists()
    assert (host_dir / ".processed" / "openclaw.jsonl").exists()
    assert [(sid, status) for sid, status, _ in _summary_jobs(db_path)] == [
        ("sess-after-ingest-lock", "pending")
    ]


def _write_live_session_event(jsonl_path: Path, event_id: str, minute: int) -> None:
    jsonl_path.write_text(
        json.dumps(
            {
                "id": event_id,
                "session_id": "sess-live",
                "timestamp": f"2026-05-08T10:{minute:02d}:00Z",
                "agent_id": "test-agent",
                "event_type": "user_message",
                "message": {"role": "user", "content": f"turn {event_id}"},
            }
        )
        + "\n"
    )


def _summary_llm(prompt: str, **_kwargs) -> dict:
    return {"summary_md": "summary", "next_steps_md": "next"}


def test_enqueue_waits_for_a_worker_completing_the_same_session(
    pg_lh, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#308: the watcher enqueues generation N+1 while the worker commits N.

    On DuckDB the two transactions conflicted on the summarize_jobs row
    ("Conflict on tuple deletion!") and the file was left to be re-parsed;
    an in-process writer lock serialized them. On the PostgreSQL ledger the
    worker's ``complete`` holds the job's row lock until its commit, and the
    enqueue's ``SELECT ... FOR UPDATE`` waits on it: N's summary lands, and
    N+1 is queued behind it rather than being overwritten by N's completion.
    """
    from drover.server.memory_store import MemoryRepository
    from drover.server.summarizer.worker import SummarizerWorker

    incoming, parquet_dir, db_path = pg_lh
    host_dir = incoming / "macmini-claude"
    host_dir.mkdir()
    handler = _Handler(parquet_dir, db_path, max_lock_retries=0)
    first = host_dir / "batch-1.jsonl"
    _write_live_session_event(first, "live-1", 0)
    handler._maybe_ingest(first)
    assert (host_dir / ".processed" / "batch-1.jsonl").exists()
    first_version = _source_version(db_path, "sess-live")

    inside_completion = threading.Event()
    release_completion = threading.Event()
    real_put_summary = MemoryRepository.put_summary

    def held_completion(con, summary):
        # Runs inside the worker's open completion transaction, after
        # ``complete`` has locked the summarize job row.
        inside_completion.set()
        assert release_completion.wait(10)
        return real_put_summary(con, summary)

    monkeypatch.setattr(MemoryRepository, "put_summary", staticmethod(held_completion))
    worker = SummarizerWorker(duckdb_path=db_path, _llm_call=_summary_llm)
    drained: list[int] = []
    worker_thread = threading.Thread(target=lambda: drained.append(worker.drain_once()))
    worker_thread.start()
    assert inside_completion.wait(10), "worker never reached its completion"

    second = host_dir / "batch-2.jsonl"
    _write_live_session_event(second, "live-2", 1)
    watcher_thread = threading.Thread(target=handler._maybe_ingest, args=(second,))
    watcher_thread.start()
    # The enqueue waits on the job's row lock while the worker is still
    # inside its transaction, then proceeds once it commits.
    watcher_thread.join(timeout=0.5)
    release_completion.set()
    worker_thread.join(10)
    watcher_thread.join(10)
    assert not worker_thread.is_alive() and not watcher_thread.is_alive()

    assert "ingest failed" not in caplog.text
    assert "summary enqueue failed" not in caplog.text
    assert not second.exists(), "conflict left the batch to be re-parsed"
    assert (host_dir / ".processed" / "batch-2.jsonl").exists()
    assert drained == [1]
    current = _source_version(db_path, "sess-live")
    assert current != first_version
    summary = MemoryRepository(db_path).summary("sess-live")
    assert summary is not None and summary.source_version == first_version
    assert _summary_jobs(db_path) == [
        ("sess-live", "succeeded", first_version),
        ("sess-live", "pending", current),
    ]


def test_failed_enqueue_does_not_fail_a_committed_ingest(
    pg_lh, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#308: the events are committed, so one failed enqueue is logged and skipped.

    Re-parsing the file would only dedupe the same rows and retry the same
    enqueue. The file moves on, the failure names its session, and the other
    sessions in the batch are still enqueued; a later batch (here, a
    redelivered copy) enqueues the one that was missed.
    """
    import drover.server.watcher as watcher_module

    incoming, parquet_dir, db_path = pg_lh
    host_dir = incoming / "macmini-claude"
    host_dir.mkdir()
    batch = host_dir / "batch.jsonl"
    _write_two_session_events(batch)
    real_enqueue = watcher_module.enqueue_summary_generation

    def fails_for_w1(store_path, session_id, source_version):
        if session_id == "sess-w1":
            raise RuntimeError("control store connection reset")
        return real_enqueue(store_path, session_id, source_version)

    monkeypatch.setattr(watcher_module, "enqueue_summary_generation", fails_for_w1)
    handler = _Handler(parquet_dir, db_path, max_lock_retries=0)

    with caplog.at_level(logging.WARNING, logger="drover.watcher"):
        handler._maybe_ingest(batch)
    assert not batch.exists(), "a failed enqueue must not leave the file for re-parse"
    assert (host_dir / ".processed" / "batch.jsonl").exists()
    assert "ingest failed" not in caplog.text
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("sess-w1" in r.getMessage() for r in warnings)
    assert [sid for sid, _, _ in _summary_jobs(db_path)] == ["sess-w2"]

    monkeypatch.setattr(watcher_module, "enqueue_summary_generation", real_enqueue)
    again = host_dir / "batch-redelivered.jsonl"
    _write_two_session_events(again)
    handler._maybe_ingest(again)
    assert (host_dir / ".processed" / "batch-redelivered.jsonl").exists()

    con = duckdb.connect(str(db_path))
    try:
        events = con.execute("""SELECT id, count(*) FROM agent_events
                WHERE session_id IN ('sess-w1', 'sess-w2')
                GROUP BY id ORDER BY id""").fetchall()
    finally:
        con.close()
    assert events == [("watcher-s1-001", 1), ("watcher-s2-001", 1)]
    versions = {sid: _source_version(db_path, sid) for sid in ("sess-w1", "sess-w2")}
    assert _summary_jobs(db_path) == [
        ("sess-w1", "pending", versions["sess-w1"]),
        ("sess-w2", "pending", versions["sess-w2"]),
    ]


def test_handler_leaves_file_in_place_when_duckdb_lock_retries_exhaust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    incoming = tmp_path / "incoming"
    host_dir = incoming / "nas-claude"
    host_dir.mkdir(parents=True)
    batch = host_dir / "openclaw.jsonl"
    batch.write_text("{}\n")

    def fake_ingest_file(
        path: Path, *, parquet_dir: Path, duckdb_path: Path, shadow_publisher=None
    ):
        raise duckdb.IOException(
            "Could not set lock on file drover.duckdb: Conflicting lock is held"
        )

    monkeypatch.setattr("drover.server.watcher.ingest_file", fake_ingest_file)
    monkeypatch.setattr("drover.server.watcher.time.sleep", lambda _seconds: None)
    handler = _Handler(
        parquet_dir=tmp_path / "parquet",
        duckdb_path=tmp_path / "drover.duckdb",
        max_lock_retries=1,
        lock_retry_base_seconds=0,
    )

    handler._maybe_ingest(batch)

    assert batch.exists(), "failed ingest must not move source file"
    assert not (host_dir / ".processed" / "openclaw.jsonl").exists()
    assert "leaving file in place" in caplog.text
    assert "runtime-audit" in caplog.text


# -- retention on the processed spool ---------------------------------------
#
# `processed_retention_days` has been in the config, in DroverConfig and in the
# documented example since the beginning, and nothing has ever enforced it. On
# the hub that meant 9.7GB of incoming, of which 8.6GB was 6,692 audit copies
# older than the seven days the operator had asked for. A policy nobody
# implements is worse than no policy: it is written down, so it is trusted.


def test_sweep_removes_processed_files_past_the_retention_window(tmp_path):
    from drover.server.watcher import sweep_processed, sweep_receipts

    incoming = tmp_path / "incoming"
    processed = incoming / "nas-claude" / ".processed"
    processed.mkdir(parents=True)

    old = processed / "old.jsonl"
    old.write_text('{"a": 1}\n')
    recent = processed / "recent.jsonl"
    recent.write_text('{"a": 2}\n')

    eight_days = time.time() - 8 * 86400
    os.utime(old, (eight_days, eight_days))

    removed = sweep_processed(incoming, retention_days=7)

    assert not old.exists(), "an audit copy past the window should be reclaimed"
    assert recent.exists(), "a copy inside the window must be kept"
    assert removed.files == 1
    assert removed.bytes > 0


def test_sweep_never_touches_anything_awaiting_ingestion(tmp_path):
    """Only `.processed` is audit. Everything else is data that has not landed.

    The watcher moves a file into `.processed` only after ingest *and* job
    enqueue succeed, so that directory is the one place where deleting cannot
    lose anything. A file sitting in the spool is still waiting to be read.
    """

    from drover.server.watcher import sweep_processed, sweep_receipts

    incoming = tmp_path / "incoming"
    host = incoming / "nas-claude"
    (host / ".processed").mkdir(parents=True)
    pending = host / "pending.jsonl"
    pending.write_text('{"a": 1}\n')
    eight_days = time.time() - 8 * 86400
    os.utime(pending, (eight_days, eight_days))

    removed = sweep_processed(incoming, retention_days=7)

    assert pending.exists(), "an un-ingested file must never be swept"
    assert removed.files == 0


def test_sweep_of_zero_days_is_a_no_op_rather_than_deleting_everything(tmp_path):
    """A misread config must not become an erase.

    Zero is the value an operator reaches for meaning "do not keep any", and
    it is also what an unset or malformed setting parses to. Treating it as
    "delete the entire audit trail" makes the failure mode of a typo
    unrecoverable, so it is declined instead.
    """

    from drover.server.watcher import sweep_processed, sweep_receipts

    incoming = tmp_path / "incoming"
    processed = incoming / "nas-claude" / ".processed"
    processed.mkdir(parents=True)
    old = processed / "old.jsonl"
    old.write_text('{"a": 1}\n')
    eight_days = time.time() - 8 * 86400
    os.utime(old, (eight_days, eight_days))

    removed = sweep_processed(incoming, retention_days=0)

    assert old.exists()
    assert removed.files == 0


def _seeded_receipt_store(tmp_path: Path):
    """A store holding receipts of every kind, old and new."""
    from drover.schema import bootstrap

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "lake", duckdb_path=duckdb_path)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recent = datetime.now(timezone.utc)

    con = duckdb.connect(str(duckdb_path))
    try:
        rows = [
            ("old-agent", "agent_event", "k1", old),
            ("old-span", "otlp_span", "k2", old),
            ("new-agent", "agent_event", "k3", recent),
            ("old-advisory", "advisory_target_snapshot", "k4", old),
            ("old-referenced", "agent_event", "k5", old),
            ("old-unknown-kind", "session_close", "k6", old),
        ]
        for receipt_id, kind, key, seen in rows:
            con.execute(
                "INSERT INTO pipeline_receipts (receipt_id, source_kind, source_key, "
                "source_version, status, first_seen_at) VALUES (?, ?, ?, '', 'observed', ?)",
                [receipt_id, kind, key, seen],
            )
        con.execute(
            "INSERT INTO pipeline_jobs (job_id, job_kind, subject_key, status, "
            "attempt_count, max_attempts, caused_by_receipt_id) "
            "VALUES ('j1', 'summarize', 'k5', 'pending', 0, 3, 'old-referenced')"
        )
    finally:
        con.close()
    return duckdb_path


def _receipt_ids(duckdb_path: Path) -> set:
    con = duckdb.connect(str(duckdb_path))
    try:
        return {
            r[0]
            for r in con.execute("SELECT receipt_id FROM pipeline_receipts").fetchall()
        }
    finally:
        con.close()


def test_receipt_sweep_reclaims_only_the_kinds_nothing_reads(tmp_path: Path) -> None:
    """`agent_event` and `otlp_span` receipts are written and never read.

    Their authoritative dedup is the Parquet partition, consulted by ingest
    before the receipt is written at all, so removing one does not make its
    source unit reprocessable. Every other kind is kept: the list is an
    allow-list precisely so a new `source_kind` defaults to being retained.
    """
    duckdb_path = _seeded_receipt_store(tmp_path)

    result = sweep_receipts(duckdb_path, retention_days=7)

    assert result.receipts == 2
    assert _receipt_ids(duckdb_path) == {
        "new-agent",
        "old-advisory",
        "old-referenced",
        "old-unknown-kind",
    }


def test_receipt_sweep_keeps_anything_a_job_still_points_at(tmp_path: Path) -> None:
    """A receipt named by `caused_by_receipt_id` is load-bearing whatever its kind.

    The advisory worker joins jobs to receipts through that column. Deleting
    the receipt would not fail loudly; it would make the join return nothing.
    """
    duckdb_path = _seeded_receipt_store(tmp_path)

    sweep_receipts(duckdb_path, retention_days=7)

    assert "old-referenced" in _receipt_ids(duckdb_path)


def test_receipt_sweep_declines_rather_than_deleting_everything(tmp_path: Path) -> None:
    """Zero means keep, the same as `processed_retention_days`.

    Zero is what an operator reaches for meaning "keep nothing", and equally
    what a malformed setting parses to. The failure mode of a typo must not be
    an erased ledger.
    """
    duckdb_path = _seeded_receipt_store(tmp_path)
    before = _receipt_ids(duckdb_path)

    assert sweep_receipts(duckdb_path, retention_days=0).receipts == 0
    assert sweep_receipts(duckdb_path, retention_days=-1).receipts == 0
    assert _receipt_ids(duckdb_path) == before


def _seeded_occurrence_store(tmp_path: Path):
    """A control-plane store holding occurrences of varying age per finding."""
    from drover.schema import bootstrap

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "lake", duckdb_path=duckdb_path)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ancient = datetime(2020, 1, 1, tzinfo=timezone.utc)
    recent = datetime.now(timezone.utc)

    con = duckdb.connect(str(control_plane_path(duckdb_path)))
    try:
        rows = [
            # finding-a: an old passing row and no failing row at all for
            # this finding -- nothing supersedes it, so it survives even
            # though it is beyond the cutoff (acceptable: nothing reads a
            # passing-only finding's occurrence history for the
            # dismissal-regression check).
            ("occ-a-old-passing", "finding-a", "run-1", "passing", old, old),
            # finding-b: an ancient failing row that is the *newest* failing
            # row for finding-b -- survives however old it is, because the
            # dismissal-regression check and material-change detection read
            # the latest failing row per finding.
            (
                "occ-b-ancient-failing",
                "finding-b",
                "run-1",
                "failing",
                ancient,
                ancient,
            ),
            # finding-c: an old failing row superseded by a newer failing
            # row of the same finding -- beyond the cutoff and superseded.
            ("occ-c-old-failing", "finding-c", "run-1", "failing", old, old),
            ("occ-c-new-failing", "finding-c", "run-2", "failing", recent, recent),
            # finding-d: recorded well within the retention window --
            # untouched regardless of outcome.
            ("occ-d-recent", "finding-d", "run-1", "passing", recent, recent),
        ]
        for (
            occurrence_id,
            finding_id,
            run_id,
            outcome,
            observed_at,
            recorded_at,
        ) in rows:
            con.execute(
                "INSERT INTO advisory_occurrences (occurrence_id, finding_id, "
                "run_id, outcome, observed_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                [occurrence_id, finding_id, run_id, outcome, observed_at, recorded_at],
            )
    finally:
        con.close()
    return duckdb_path


def _occurrence_ids(duckdb_path: Path) -> set:
    con = duckdb.connect(str(control_plane_path(duckdb_path)))
    try:
        return {
            r[0]
            for r in con.execute(
                "SELECT occurrence_id FROM advisory_occurrences"
            ).fetchall()
        }
    finally:
        con.close()


def test_advisory_occurrence_sweep_reclaims_old_superseded_rows(tmp_path: Path) -> None:
    """A row is only reclaimed when it is both beyond the cutoff *and*
    superseded by a later failing occurrence of the same finding.

    ``occ-c-old-failing`` is the only row here that is both: beyond the
    cutoff, and superseded by ``occ-c-new-failing``. ``occ-a-old-passing``
    is beyond the cutoff too, but nothing supersedes it (its finding has no
    failing row at all), so it survives.
    """
    duckdb_path = _seeded_occurrence_store(tmp_path)

    result = sweep_advisory_occurrences(duckdb_path, retention_days=7)

    assert result.occurrences == 1
    assert _occurrence_ids(duckdb_path) == {
        "occ-a-old-passing",
        "occ-b-ancient-failing",
        "occ-c-new-failing",
        "occ-d-recent",
    }


def test_advisory_occurrence_sweep_releases_control_window_before_finishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large retention pass must leave a window for fleet reads (#388)."""
    import drover.server.watcher as watcher

    duckdb_path = _seeded_occurrence_store(tmp_path)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    con = duckdb.connect(str(control_plane_path(duckdb_path)))
    try:
        con.executemany(
            "INSERT INTO advisory_occurrences "
            "(occurrence_id, finding_id, run_id, outcome, observed_at, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (f"occ-c-extra-{i:03d}", "finding-c", "run-old", "failing", old, old)
                for i in range(260)
            ],
        )
    finally:
        con.close()

    real_connection = watcher.control_plane_connection
    remaining_after_window: list[int] = []
    reader_counts: list[int] = []
    reader_errors: list[Exception] = []
    reader: threading.Thread | None = None
    candidate_scans = 0

    def fleet_read() -> None:
        try:
            with real_connection(duckdb_path) as connection:
                reader_counts.append(
                    int(
                        connection.execute(
                            "SELECT count(*) FROM advisory_occurrences"
                        ).fetchone()[0]
                    )
                )
        except Exception as exc:
            reader_errors.append(exc)

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, params):
            nonlocal reader, candidate_scans
            if "SELECT o.occurrence_id" in sql:
                candidate_scans += 1
            if sql.lstrip().startswith("DELETE") and reader is None:
                # Queue a real fleet reader while the DELETE holds the lock.
                reader = threading.Thread(target=fleet_read)
                reader.start()
            return self.connection.execute(sql, params)

    @contextmanager
    def observe_control_window(path: Path):
        with real_connection(path) as connection:
            yield ObservedConnection(connection)
        if reader is not None and not reader_counts:
            reader.join(timeout=5)
        remaining_after_window.append(len(_occurrence_ids(duckdb_path)))

    monkeypatch.setattr(watcher, "control_plane_connection", observe_control_window)
    result = sweep_advisory_occurrences(duckdb_path, retention_days=7)

    assert result.occurrences == 261
    assert candidate_scans == 1
    assert not reader_errors
    assert len(reader_counts) == 1
    assert 4 < reader_counts[0] < 265
    assert any(4 < remaining < 265 for remaining in remaining_after_window)
    assert remaining_after_window[-1] == 4


def test_advisory_occurrence_candidate_scan_stays_short_for_one_busy_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One finding's history must not make the fleet wait on a quadratic scan."""
    import drover.server.watcher as watcher

    duckdb_path = _seeded_occurrence_store(tmp_path)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recent = datetime.now(timezone.utc)
    con = duckdb.connect(str(control_plane_path(duckdb_path)))
    try:
        con.execute(
            "INSERT INTO advisory_occurrences "
            "(occurrence_id, finding_id, run_id, outcome, observed_at, recorded_at) "
            "VALUES ('busy-newest', 'finding-busy', 'run-new', 'failing', ?, ?)",
            [recent, recent],
        )
        con.execute(
            "INSERT INTO advisory_occurrences "
            "(occurrence_id, finding_id, run_id, outcome, observed_at, recorded_at) "
            "SELECT 'busy-old-' || i::VARCHAR, 'finding-busy', 'run-old', "
            "'failing', ?, ? FROM range(20000) t(i)",
            [old, old],
        )
    finally:
        con.close()

    real_connection = watcher.control_plane_connection
    scan_seconds: list[float] = []

    class StopAfterScan(Exception):
        pass

    @contextmanager
    def observe_first_window(path: Path):
        start = time.perf_counter()
        with real_connection(path) as connection:
            yield connection
        scan_seconds.append(time.perf_counter() - start)
        raise StopAfterScan

    monkeypatch.setattr(watcher, "control_plane_connection", observe_first_window)
    with pytest.raises(StopAfterScan):
        sweep_advisory_occurrences(duckdb_path, retention_days=7)

    assert scan_seconds[0] < 2.0


def test_advisory_occurrence_sweep_keeps_newest_failing_occurrence_however_old(
    tmp_path: Path,
) -> None:
    """The dismissal-regression check and material-change detection both read
    the latest failing occurrence per finding (repository.py's
    ``_next_observed_state``), so it must never be swept regardless of age.
    """
    duckdb_path = _seeded_occurrence_store(tmp_path)

    sweep_advisory_occurrences(duckdb_path, retention_days=7)

    assert "occ-b-ancient-failing" in _occurrence_ids(duckdb_path)


def test_advisory_occurrence_sweep_uses_id_to_break_failing_timestamp_ties(
    tmp_path: Path,
) -> None:
    duckdb_path = _seeded_occurrence_store(tmp_path)
    failing_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    passing_at = datetime(2021, 1, 1, tzinfo=timezone.utc)
    con = duckdb.connect(str(control_plane_path(duckdb_path)))
    try:
        con.executemany(
            "INSERT INTO advisory_occurrences "
            "(occurrence_id, finding_id, run_id, outcome, observed_at, recorded_at) "
            "VALUES (?, 'finding-tie', 'run', ?, ?, ?)",
            [
                ("tie-a", "failing", failing_at, failing_at),
                ("tie-z", "failing", failing_at, failing_at),
                ("tie-passing-later", "passing", passing_at, passing_at),
            ],
        )
    finally:
        con.close()

    result = sweep_advisory_occurrences(duckdb_path, retention_days=7)

    assert result.occurrences == 2  # tie-a and the fixture's occ-c-old-failing
    ids = _occurrence_ids(duckdb_path)
    assert "tie-a" not in ids
    assert {"tie-z", "tie-passing-later"} <= ids


def test_advisory_occurrence_sweep_leaves_rows_younger_than_cutoff(
    tmp_path: Path,
) -> None:
    duckdb_path = _seeded_occurrence_store(tmp_path)

    sweep_advisory_occurrences(duckdb_path, retention_days=7)

    assert "occ-d-recent" in _occurrence_ids(duckdb_path)


def test_advisory_occurrence_sweep_declines_rather_than_deleting_everything(
    tmp_path: Path,
) -> None:
    """Zero (and a malformed negative) means keep, matching `sweep_receipts`."""
    duckdb_path = _seeded_occurrence_store(tmp_path)
    before = _occurrence_ids(duckdb_path)

    assert sweep_advisory_occurrences(duckdb_path, retention_days=0).occurrences == 0
    assert sweep_advisory_occurrences(duckdb_path, retention_days=-1).occurrences == 0
    assert _occurrence_ids(duckdb_path) == before


def test_bootstrap_control_plane_store_indexes_advisory_occurrences(
    tmp_path: Path,
) -> None:
    from drover.schema import bootstrap

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "lake", duckdb_path=duckdb_path)

    con = duckdb.connect(str(control_plane_path(duckdb_path)))
    try:
        names = {
            r[0]
            for r in con.execute(
                "SELECT index_name FROM duckdb_indexes() "
                "WHERE table_name = 'advisory_occurrences'"
            ).fetchall()
        }
    finally:
        con.close()
    assert "idx_advisory_occurrences_finding" in names
