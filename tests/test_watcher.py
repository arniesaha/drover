"""Tests for src/drover/server/watcher.py."""

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.db import control_plane_path
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
