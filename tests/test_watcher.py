"""Tests for src/drover/server/watcher.py."""

import json
import shutil
import time
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.watcher import IncomingWatcher, _Handler


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


def test_watcher_enqueues_summarize_jobs(lh):
    """After ingesting a JSONL with 2 distinct sessions, both must appear in summarize_jobs
    with status='pending'.  Re-ingesting the same file must not duplicate the rows."""
    incoming, parquet_dir, db_path = lh
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

        def jobs_enqueued():
            con = duckdb.connect(str(db_path))
            try:
                rows = con.execute(
                    "SELECT session_id, status FROM summarize_jobs"
                    " WHERE session_id IN ('sess-w1', 'sess-w2')"
                ).fetchall()
                return len(rows) == 2
            finally:
                con.close()

        assert _wait_for(
            jobs_enqueued
        ), "summarize_jobs never populated for new sessions"

        # Verify status values
        con = duckdb.connect(str(db_path))
        try:
            rows = con.execute(
                "SELECT session_id, status FROM summarize_jobs"
                " WHERE session_id IN ('sess-w1', 'sess-w2')"
                " ORDER BY session_id"
            ).fetchall()
        finally:
            con.close()
        assert rows == [("sess-w1", "pending"), ("sess-w2", "pending")]

        # Re-ingest: write same events under a different filename and drop it
        target2 = host_dir / "batch-multi-dup.jsonl"
        tmp2 = target2.with_suffix(".jsonl.tmp")
        _write_two_session_events(tmp2)
        tmp2.rename(target2)

        # Give the watcher time to process the duplicate file
        def dup_processed():
            return (host_dir / ".processed" / "batch-multi-dup.jsonl").exists()

        assert _wait_for(dup_processed), "duplicate batch file never processed"

        # Row count must remain exactly 2 (ON CONFLICT DO NOTHING)
        con = duckdb.connect(str(db_path))
        try:
            count = con.execute(
                "SELECT count(*) FROM summarize_jobs"
                " WHERE session_id IN ('sess-w1', 'sess-w2')"
            ).fetchone()[0]
        finally:
            con.close()
        assert (
            count == 2
        ), "re-ingesting same sessions must not create duplicate summarize_jobs"
    finally:
        w.stop()


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
        ledger_receipts = 0
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    db_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    calls = {"ingest": 0, "connect": 0}
    real_connect = duckdb.connect

    class Stats:
        read = 1
        inserted = 1
        skipped_dupes = 0
        errors = 0
        shadow_published = 0
        ledger_receipts = 0

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
    con = real_connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT session_id, status FROM summarize_jobs WHERE session_id = ?",
            ["sess-after-ingest-lock"],
        ).fetchall()
    finally:
        con.close()
    assert rows == [("sess-after-ingest-lock", "pending")]


def test_handler_publishes_summarize_jobs_to_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    host_dir = incoming / "nas-claude"
    host_dir.mkdir(parents=True)
    batch = host_dir / "openclaw.jsonl"
    batch.write_text(
        json.dumps(
            {
                "id": "event-stream-enqueue",
                "session_id": "sess-stream-enqueue",
                "timestamp": "2026-05-08T10:00:00Z",
                "agent_id": "test-agent",
                "event_type": "user_message",
                "message": {"role": "user", "content": "hi"},
            }
        )
        + "\n"
    )
    parquet_dir = tmp_path / "parquet"
    db_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    published: list[dict] = []

    class Stats:
        read = 1
        inserted = 1
        skipped_dupes = 0
        errors = 0
        shadow_published = 0
        ledger_receipts = 0
        new_session_ids = {"sess-stream-enqueue"}

    class FakeStream:
        def add(self, fields: dict) -> str:
            published.append(fields)
            return "1-0"

    def fake_ingest_file(
        path: Path, *, parquet_dir: Path, duckdb_path: Path, shadow_publisher=None
    ):
        return Stats()

    monkeypatch.setattr("drover.server.watcher.ingest_file", fake_ingest_file)
    handler = _Handler(
        parquet_dir=parquet_dir,
        duckdb_path=db_path,
        summarize_job_stream=FakeStream(),
    )

    handler._maybe_ingest(batch)

    assert len(published) == 1
    assert published[0]["session_id"] == "sess-stream-enqueue"
    assert len(published[0]["source_version"]) == 64
    assert not batch.exists()
    assert (host_dir / ".processed" / "openclaw.jsonl").exists()


def test_handler_publishes_only_when_source_generation_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    host_dir = incoming / "nas-claude"
    host_dir.mkdir(parents=True)
    batch = host_dir / "duplicate.jsonl"
    _write_event(batch, "duplicate-generation")
    parquet_dir = tmp_path / "parquet"
    db_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    published: list[dict] = []

    class Stats:
        read = 1
        inserted = 0
        skipped_dupes = 1
        errors = 0
        shadow_published = 0
        ledger_receipts = 0
        new_session_ids = {"sess-x"}

    class FakeStream:
        def add(self, fields: dict) -> str:
            published.append(fields)
            return "1-0"

    monkeypatch.setattr("drover.server.watcher.ingest_file", lambda *a, **kw: Stats())
    monkeypatch.setattr(
        "drover.server.watcher.source_version_for_session", lambda con, sid: "v1"
    )
    monkeypatch.setattr(
        "drover.server.watcher.enqueue_summary_generation",
        lambda con, sid, version: False,
    )
    handler = _Handler(
        parquet_dir=parquet_dir,
        duckdb_path=db_path,
        summarize_job_stream=FakeStream(),
    )

    handler._maybe_ingest(batch)

    assert published == []


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
