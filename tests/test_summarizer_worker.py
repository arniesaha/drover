"""Tests for SummarizerWorker — drains summarize_jobs into session_summaries."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from drover.schema import bootstrap
from drover.server.jobs import JobStream
from drover.server.summarizer.jobs import (
    enqueue_summary_generation,
    publish_summary_generation,
)
from drover.server.summarizer.worker import SummarizerWorker, _session_agent_events_ctes


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _write_events(parquet_dir: Path, session_id: str) -> None:
    now = datetime.now(timezone.utc)
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
    rows = [
        (
            "e1",
            session_id,
            "macmini-claude",
            "tid1",
            now - timedelta(minutes=2),
            "user_message",
            "user",
            "do the thing",
            "arniesaha",
            "nexus",
            "main",
            "arnab",
            f"{session_id}-k1",
            "{}",
        ),
        (
            "e2",
            session_id,
            "macmini-claude",
            "tid1",
            now - timedelta(minutes=1),
            "tool_call",
            "assistant",
            "edited foo.py",
            "arniesaha",
            "nexus",
            "main",
            "arnab",
            f"{session_id}-k2",
            json.dumps(
                {
                    "tool_use_blocks": [
                        {"name": "Edit", "input": {"file_path": "src/foo.py"}}
                    ]
                }
            ),
        ),
    ]
    cols = {f.name: [r[i] for r in rows] for i, f in enumerate(schema)}
    table = pa.table(
        {k: pa.array(v, type=schema.field(k).type) for k, v in cols.items()},
        schema=schema,
    )
    out = parquet_dir / "agent_events" / "date=2026-05-09" / f"agent_id=macmini-claude"
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / f"part-{session_id}.parquet")


def _enqueue(duckdb_path: Path, session_id: str) -> None:
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts) VALUES (?, 'pending', 0)",
            [session_id],
        )
    finally:
        con.close()


def _fake_llm_call(prompt: str, *, api_key, model, _client=None, **kw) -> dict:
    return {
        "summary_md": "Fixture summary describing the session.",
        "next_steps_md": "Move on to Plan 6.",
        "open_questions": ["use sse or streamable-http?"],
        "last_user_prompt": "do the thing",
        "last_assistant": "edited foo.py",
    }


def test_session_agent_events_cte_filters_before_canonical_dedupe() -> None:
    sql = _session_agent_events_ctes()

    assert "session_agent_events AS" in sql
    assert "FROM agent_events\n  WHERE session_id = ?" in sql
    assert "FROM session_agent_events ae" in sql


def test_worker_drains_one_pending_job(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-W1")
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _enqueue(duckdb_path, "sess-W1")

    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        api_key="sk-test",
        _llm_call=_fake_llm_call,
    )
    drained = worker.drain_once()

    assert drained == 1

    con = duckdb.connect(str(duckdb_path))
    try:
        rows = con.execute(
            "SELECT session_id, summary_md, status, generator_model FROM session_summaries"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "sess-W1"
        assert "Fixture summary" in rows[0][1]
        assert rows[0][2] == "completed"

        job_status = con.execute(
            "SELECT status FROM summarize_jobs WHERE session_id='sess-W1'"
        ).fetchone()
        assert job_status[0] == "done"
    finally:
        con.close()


def test_successful_summary_clears_the_dead_letter_streak(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-W1b")
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _enqueue(duckdb_path, "sess-W1b")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "UPDATE summarize_jobs SET dead_letter_streak=2 WHERE session_id='sess-W1b'"
        )
    finally:
        con.close()

    SummarizerWorker(
        duckdb_path=duckdb_path,
        api_key="sk-test",
        _llm_call=_fake_llm_call,
    ).drain_once()

    con = duckdb.connect(str(duckdb_path))
    try:
        assert con.execute(
            "SELECT status, dead_letter_streak FROM summarize_jobs "
            "WHERE session_id='sess-W1b'"
        ).fetchone() == ("done", 0)
    finally:
        con.close()


def test_worker_parks_failure_until_retry_time_when_no_api_key(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-W2")
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _enqueue(duckdb_path, "sess-W2")

    worker = SummarizerWorker(duckdb_path=duckdb_path, api_key=None)
    worker.drain_once()

    con = duckdb.connect(str(duckdb_path))
    try:
        status = con.execute(
            "SELECT status, last_error FROM summarize_jobs WHERE session_id='sess-W2'"
        ).fetchone()
        assert status[0] == "retry_wait"
        assert status[1] and "api_key" in status[1].lower()

        # No session_summaries row written
        n = con.execute("SELECT count(*) FROM session_summaries").fetchone()[0]
        assert n == 0
    finally:
        con.close()


def test_worker_handles_llm_failure_with_retry_wait(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-W3")
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _enqueue(duckdb_path, "sess-W3")

    def boom(prompt, **kw):
        raise RuntimeError("simulated network blip")

    worker = SummarizerWorker(
        duckdb_path=duckdb_path, api_key="sk-test", _llm_call=boom
    )
    worker.drain_once()

    con = duckdb.connect(str(duckdb_path))
    try:
        status = con.execute(
            "SELECT status, last_error FROM summarize_jobs WHERE session_id='sess-W3'"
        ).fetchone()
        assert status[0] == "retry_wait"
        assert "simulated network blip" in status[1]
    finally:
        con.close()


def test_drain_once_returns_zero_when_empty(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    worker = SummarizerWorker(
        duckdb_path=duckdb_path, api_key="sk-test", _llm_call=_fake_llm_call
    )
    assert worker.drain_once() == 0


class _StubBackend:
    name = "stub"
    model = "stub-model-v1"

    def __init__(self):
        self.calls = 0
        self.ensure_calls = 0

    def ensure_ready(self) -> None:
        self.ensure_calls += 1

    def summarize(self, prompt: str) -> dict:
        self.calls += 1
        return {
            "summary_md": f"backend summary #{self.calls}",
            "next_steps_md": "next",
            "open_questions": [],
        }


def test_worker_uses_backend_when_provided(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-B1")
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _enqueue(duckdb_path, "sess-B1")

    backend = _StubBackend()
    worker = SummarizerWorker(duckdb_path=duckdb_path, backend=backend)
    drained = worker.drain_once()
    assert drained == 1
    assert backend.calls == 1
    # ensure_ready called once at batch start
    assert backend.ensure_calls == 1

    con = duckdb.connect(str(duckdb_path))
    try:
        row = con.execute(
            "SELECT summary_md, generator_model FROM session_summaries WHERE session_id='sess-B1'"
        ).fetchone()
    finally:
        con.close()
    assert row[0] == "backend summary #1"
    # generator_model came from backend.model, not worker.model
    assert row[1] == "stub-model-v1"


def test_worker_drain_batch_processes_multiple_jobs_with_one_warmup(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    for sid in ("sess-B2", "sess-B3", "sess-B4"):
        _write_events(parquet_dir, sid)
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    for sid in ("sess-B2", "sess-B3", "sess-B4"):
        _enqueue(duckdb_path, sid)

    backend = _StubBackend()
    worker = SummarizerWorker(duckdb_path=duckdb_path, backend=backend, batch_size=10)
    drained = worker.drain_batch()
    assert drained == 3
    assert backend.calls == 3
    # ensure_ready fires once per drain_batch call, not per job
    assert backend.ensure_calls == 1


def test_worker_drain_batch_stops_when_queue_empty(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-B5")
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _enqueue(duckdb_path, "sess-B5")

    backend = _StubBackend()
    worker = SummarizerWorker(duckdb_path=duckdb_path, backend=backend, batch_size=20)
    drained = worker.drain_batch()
    # Only one job enqueued — drain_batch should not loop past the empty queue
    assert drained == 1
    assert backend.calls == 1


def test_worker_writes_deterministic_files_touched(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-W4")
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _enqueue(duckdb_path, "sess-W4")

    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        api_key="sk-test",
        _llm_call=_fake_llm_call,
    )
    worker.drain_once()

    con = duckdb.connect(str(duckdb_path))
    try:
        files, tools = con.execute(
            "SELECT files_touched, tools_used FROM session_summaries WHERE session_id='sess-W4'"
        ).fetchone()
        assert files == ["src/foo.py"]
        assert tools == {"Edit": 1}
    finally:
        con.close()


def test_worker_acks_stream_job_after_durable_summary_write(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-stream-ok")
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _enqueue(duckdb_path, "sess-stream-ok")
    stream = JobStream("summarize_jobs")
    stream.add({"session_id": "sess-stream-ok"})

    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        api_key="sk-test",
        _llm_call=_fake_llm_call,
        job_stream=stream,
        worker_id="worker-a",
    )

    assert worker.drain_once() == 1
    assert stream.pending() == []
    assert stream.length() == 0


def test_stream_worker_does_not_resolve_backend_when_idle(tmp_path: Path) -> None:
    _parquet_dir, duckdb_path = _seed(tmp_path)
    stream = JobStream("summarize_jobs")

    class FailingBackend:
        def ensure_ready(self):
            raise AssertionError("backend should not be warmed on idle stream tick")

    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        api_key="sk-test",
        backend=FailingBackend(),
        job_stream=stream,
        worker_id="worker-idle",
    )

    assert worker.drain_once() == 0


def test_worker_leaves_failed_stream_job_unacked_for_reclaim(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-stream-fail")
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _enqueue(duckdb_path, "sess-stream-fail")
    start = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

    class StreamClock:
        now = int(start.timestamp() * 1000)

        def __call__(self) -> int:
            return self.now

    stream_clock = StreamClock()
    stream = JobStream("summarize_jobs", visibility_timeout_ms=0, clock=stream_clock)
    stream.add({"session_id": "sess-stream-fail"})

    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        api_key=None,
        job_stream=stream,
        worker_id="worker-a",
        _clock=lambda: start,
        _jitter=lambda _low, _high: 0,
    )

    assert worker.drain_once() == 1
    pending = stream.pending()
    assert len(pending) == 1
    assert pending[0].last_error and "api_key" in pending[0].last_error.lower()
    assert stream.reclaim("worker-b") == []
    stream_clock.now += 60_000
    reclaimed = stream.reclaim("worker-b")
    assert len(reclaimed) == 1
    assert reclaimed[0].fields["session_id"] == "sess-stream-fail"


def test_worker_acks_redelivery_when_summary_already_done(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _enqueue(duckdb_path, "sess-stream-written")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "UPDATE summarize_jobs SET status='done' WHERE session_id='sess-stream-written'"
        )
    finally:
        con.close()
    stream = JobStream("summarize_jobs", visibility_timeout_ms=0)
    stream.add({"session_id": "sess-stream-written"})

    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        api_key="sk-test",
        _llm_call=_fake_llm_call,
        job_stream=stream,
        worker_id="worker-b",
    )

    assert worker.drain_once() == 1
    assert stream.pending() == []
    assert stream.length() == 0


def test_fifth_failure_dead_letters_serving_and_pipeline_job(tmp_path: Path) -> None:
    _parquet_dir, duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs "
            "(session_id, status, attempts, source_version) "
            "VALUES ('s1', 'running', 4, 'v1')"
        )
    finally:
        con.close()
    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        _clock=lambda: datetime(2026, 8, 6, tzinfo=timezone.utc),
        _jitter=lambda _low, _high: 0,
    )

    worker._finish_failure("s1", "v1", RuntimeError("backend failed"))

    con = duckdb.connect(str(duckdb_path))
    try:
        assert con.execute(
            "SELECT status,attempts FROM summarize_jobs WHERE session_id='s1'"
        ).fetchone() == ("dead_lettered", 5)
        assert con.execute(
            "SELECT status FROM pipeline_jobs "
            "WHERE job_kind='summarize_session' AND subject_key='s1'"
        ).fetchone() == ("dead_lettered",)
    finally:
        con.close()


def test_duckdb_worker_honors_retry_wait_until_due(tmp_path: Path) -> None:
    _parquet_dir, duckdb_path = _seed(tmp_path)
    due = datetime(2026, 8, 6, 12, 5)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs "
            "(session_id, status, attempts, source_version, next_run_at) "
            "VALUES ('s1', 'retry_wait', 1, 'v1', ?)",
            [due],
        )
    finally:
        con.close()
    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        _clock=lambda: datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
    )

    assert worker._claim_duckdb_job() is None

    worker._clock = lambda: datetime(2026, 8, 6, 12, 6, tzinfo=timezone.utc)
    claim = worker._claim_duckdb_job()
    assert claim is not None
    assert claim[0] == "s1"


def test_stream_worker_acks_stale_source_generation_without_backend_call(
    tmp_path: Path,
) -> None:
    _parquet_dir, duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, source_version) "
            "VALUES ('s1', 'pending', 'v2')"
        )
    finally:
        con.close()
    stream = JobStream("summarize_jobs")
    stream.add({"session_id": "s1", "source_version": "v1"})
    backend = _StubBackend()
    worker = SummarizerWorker(
        duckdb_path=duckdb_path, backend=backend, job_stream=stream
    )

    assert worker.drain_once() == 1
    assert backend.calls == 0
    assert stream.pending() == []


def test_stream_worker_acks_dead_lettered_generation_without_backend_call(
    tmp_path: Path,
) -> None:
    _parquet_dir, duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs "
            "(session_id, status, attempts, source_version, dead_lettered_at) "
            "VALUES ('s1', 'dead_lettered', 5, 'v1', now())"
        )
    finally:
        con.close()
    stream = JobStream("summarize_jobs")
    stream.add({"session_id": "s1", "source_version": "v1"})
    backend = _StubBackend()
    worker = SummarizerWorker(
        duckdb_path=duckdb_path, backend=backend, job_stream=stream
    )

    assert worker.drain_once() == 1
    assert backend.calls == 0
    assert stream.pending() == []


def test_stream_worker_leaves_retry_wait_unacked_until_due(tmp_path: Path) -> None:
    _parquet_dir, duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs "
            "(session_id, status, attempts, source_version, next_run_at) "
            "VALUES ('s1', 'retry_wait', 1, 'v1', ?)",
            [datetime(2026, 8, 6, 12, 5)],
        )
    finally:
        con.close()
    stream = JobStream("summarize_jobs", visibility_timeout_ms=0)
    stream.add({"session_id": "s1", "source_version": "v1"})
    backend = _StubBackend()
    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        backend=backend,
        job_stream=stream,
        _clock=lambda: datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
    )

    assert worker.drain_once() == 0
    assert backend.calls == 0
    assert len(stream.pending()) == 1


def test_stream_retry_backoff_allows_exactly_five_backend_executions(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "s1")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, source_version) "
            "VALUES ('s1', 'pending', 'v1')"
        )
    finally:
        con.close()

    start = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

    class MillisecondClock:
        now = int(start.timestamp() * 1000)

        def __call__(self) -> int:
            return self.now

        def advance(self, milliseconds: int) -> None:
            self.now += milliseconds

    class AlwaysFailBackend(_StubBackend):
        def summarize(self, prompt: str) -> dict:
            self.calls += 1
            raise RuntimeError("backend failed")

    stream_clock = MillisecondClock()
    stream = JobStream(
        "summarize_jobs",
        clock=stream_clock,
        visibility_timeout_ms=60_000,
        max_deliveries=5,
    )
    stream.add({"session_id": "s1", "source_version": "v1"})
    backend = AlwaysFailBackend()
    elapsed_seconds = 0
    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        backend=backend,
        job_stream=stream,
        _clock=lambda: start + timedelta(seconds=elapsed_seconds),
        _jitter=lambda _low, _high: 0,
    )

    assert worker.drain_once() == 1
    ticks = 0
    while backend.calls < 5 and ticks < 30:
        ticks += 1
        elapsed_seconds += 60
        stream_clock.advance(60_000)
        worker.drain_once()

    assert backend.calls == 5
    assert stream.dead_letters() == []
    assert stream.pending() == []
    con = duckdb.connect(str(duckdb_path))
    try:
        assert con.execute(
            "SELECT status, attempts FROM summarize_jobs WHERE session_id='s1'"
        ).fetchone() == ("dead_lettered", 5)
    finally:
        con.close()


def test_stale_inflight_success_cannot_overwrite_new_generation(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "s1")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, source_version) "
            "VALUES ('s1', 'pending', 'v1')"
        )
    finally:
        con.close()

    class SupersedingBackend(_StubBackend):
        def summarize(self, prompt: str) -> dict:
            self.calls += 1
            con = duckdb.connect(str(duckdb_path))
            try:
                enqueue_summary_generation(con, "s1", "v2")
            finally:
                con.close()
            return super().summarize(prompt)

    stream = JobStream("summarize_jobs")
    stream.add({"session_id": "s1", "source_version": "v1"})
    brief_stream = JobStream("brief_jobs")
    embed_stream = JobStream("embed_jobs")
    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        backend=SupersedingBackend(),
        job_stream=stream,
        brief_job_stream=brief_stream,
        embed_job_stream=embed_stream,
    )

    assert worker.drain_once() == 1
    con = duckdb.connect(str(duckdb_path))
    try:
        assert con.execute(
            "SELECT source_version, status, attempts FROM summarize_jobs "
            "WHERE session_id='s1'"
        ).fetchone() == ("v2", "pending", 0)
        assert con.execute(
            "SELECT count(*) FROM session_summaries WHERE session_id='s1'"
        ).fetchone() == (0,)
        assert con.execute(
            "SELECT status FROM pipeline_jobs WHERE subject_key='s1'"
        ).fetchone() == ("dead_lettered",)
    finally:
        con.close()
    assert stream.pending() == []
    assert brief_stream.length() == 0
    assert embed_stream.length() == 0


def test_stale_inflight_failure_acks_and_closes_ledger_attempt(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "s1")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, source_version) "
            "VALUES ('s1', 'pending', 'v1')"
        )
    finally:
        con.close()

    class SupersedingFailureBackend(_StubBackend):
        def summarize(self, prompt: str) -> dict:
            self.calls += 1
            con = duckdb.connect(str(duckdb_path))
            try:
                enqueue_summary_generation(con, "s1", "v2")
            finally:
                con.close()
            raise RuntimeError("obsolete backend failure")

    stream = JobStream("summarize_jobs")
    stream.add({"session_id": "s1", "source_version": "v1"})
    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        backend=SupersedingFailureBackend(),
        job_stream=stream,
    )

    assert worker.drain_once() == 1
    con = duckdb.connect(str(duckdb_path))
    try:
        assert con.execute(
            "SELECT source_version, status, attempts FROM summarize_jobs "
            "WHERE session_id='s1'"
        ).fetchone() == ("v2", "pending", 0)
        assert con.execute(
            "SELECT status FROM pipeline_jobs WHERE subject_key='s1'"
        ).fetchone() == ("dead_lettered",)
    finally:
        con.close()
    assert stream.pending() == []


def test_generation_change_at_post_persist_seam_blocks_all_success_effects(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "s1")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, source_version) "
            "VALUES ('s1', 'pending', 'v1')"
        )
    finally:
        con.close()

    def supersede_before_success_effects() -> None:
        con = duckdb.connect(str(duckdb_path))
        try:
            enqueue_summary_generation(con, "s1", "v2")
        finally:
            con.close()

    stream = JobStream("summarize_jobs")
    stream.add({"session_id": "s1", "source_version": "v1"})
    brief_stream = JobStream("brief_jobs")
    embed_stream = JobStream("embed_jobs")
    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        backend=_StubBackend(),
        job_stream=stream,
        brief_job_stream=brief_stream,
        embed_job_stream=embed_stream,
        _before_success_effects=supersede_before_success_effects,
    )

    assert worker.drain_once() == 1
    con = duckdb.connect(str(duckdb_path))
    try:
        assert con.execute(
            "SELECT source_version, status FROM summarize_jobs WHERE session_id='s1'"
        ).fetchone() == ("v2", "pending")
        assert con.execute(
            "SELECT count(*) FROM session_summaries WHERE session_id='s1'"
        ).fetchone() == (0,)
        assert con.execute(
            "SELECT status FROM pipeline_jobs WHERE subject_key='s1'"
        ).fetchone() == ("dead_lettered",)
        assert con.execute("SELECT count(*) FROM brief_jobs").fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM embed_jobs").fetchone() == (0,)
    finally:
        con.close()
    assert brief_stream.length() == 0
    assert embed_stream.length() == 0
    assert stream.pending() == []


def test_generation_change_at_failure_finish_seam_is_atomically_stale(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "s1")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, source_version) "
            "VALUES ('s1', 'pending', 'v1')"
        )
    finally:
        con.close()

    def supersede_before_failure_finish() -> None:
        con = duckdb.connect(str(duckdb_path))
        try:
            enqueue_summary_generation(con, "s1", "v2")
        finally:
            con.close()

    class FailingBackend(_StubBackend):
        def summarize(self, prompt: str) -> dict:
            self.calls += 1
            raise RuntimeError("backend failed")

    stream = JobStream("summarize_jobs")
    stream.add({"session_id": "s1", "source_version": "v1"})
    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        backend=FailingBackend(),
        job_stream=stream,
        _before_failure_finish=supersede_before_failure_finish,
    )

    assert worker.drain_once() == 1
    con = duckdb.connect(str(duckdb_path))
    try:
        assert con.execute(
            "SELECT source_version, status, attempts FROM summarize_jobs "
            "WHERE session_id='s1'"
        ).fetchone() == ("v2", "pending", 0)
        assert con.execute(
            "SELECT status FROM pipeline_jobs WHERE subject_key='s1'"
        ).fetchone() == ("dead_lettered",)
    finally:
        con.close()
    assert stream.pending() == []


def test_generation_change_after_commit_suppresses_old_downstream_publication(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "s1")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, source_version) "
            "VALUES ('s1', 'pending', 'v1')"
        )
    finally:
        con.close()

    summarize_stream = JobStream("summarize_jobs")
    summarize_stream.add({"session_id": "s1", "source_version": "v1"})

    def supersede_after_commit() -> None:
        con = duckdb.connect(str(duckdb_path))
        try:
            assert enqueue_summary_generation(con, "s1", "v2") is True
        finally:
            con.close()
        summarize_stream.add({"session_id": "s1", "source_version": "v2"})

    brief_stream = JobStream("brief_jobs")
    embed_stream = JobStream("embed_jobs")
    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        backend=_StubBackend(),
        job_stream=summarize_stream,
        brief_job_stream=brief_stream,
        embed_job_stream=embed_stream,
        _after_completion_commit=supersede_after_commit,
    )

    assert worker.drain_once() == 1
    assert brief_stream.length() == 0
    assert embed_stream.length() == 0
    con = duckdb.connect(str(duckdb_path))
    try:
        assert con.execute(
            "SELECT source_version, status FROM summarize_jobs WHERE session_id='s1'"
        ).fetchone() == ("v2", "pending")
        assert con.execute(
            "SELECT source_version, status FROM embed_jobs WHERE session_id='s1'"
        ).fetchone() == ("v1", "superseded")
        assert con.execute(
            "SELECT source_session_id, source_version, status FROM brief_jobs"
        ).fetchone() == ("s1", "v1", "superseded")
    finally:
        con.close()

    (v2_delivery,) = summarize_stream.read_group("next-worker")
    assert v2_delivery.fields == {"session_id": "s1", "source_version": "v2"}


def test_stream_poll_recovers_durable_publish_without_producer_reentry(
    tmp_path: Path,
) -> None:
    _, duckdb_path = _seed(tmp_path)

    class FlakyIdleStream:
        def __init__(self) -> None:
            self.add_calls = 0
            self.published: list[dict] = []

        def add(self, fields: dict) -> str:
            self.add_calls += 1
            if self.add_calls == 1:
                raise RuntimeError("redis unavailable")
            self.published.append(fields)
            return "1-0"

        def read_group(self, consumer: str, count: int = 1) -> list:
            return []

        def reclaim(self, consumer: str, count: int = 1) -> list:
            return []

    stream = FlakyIdleStream()
    con = duckdb.connect(str(duckdb_path))
    try:
        assert enqueue_summary_generation(con, "s1", "v1") is True
        with pytest.raises(RuntimeError, match="redis unavailable"):
            publish_summary_generation(con, "s1", "v1", stream)
    finally:
        con.close()

    worker = SummarizerWorker(duckdb_path=duckdb_path, job_stream=stream)
    assert worker.drain_once() == 0
    assert stream.published == [{"session_id": "s1", "source_version": "v1"}]
    con = duckdb.connect(str(duckdb_path))
    try:
        assert con.execute(
            "SELECT stream_publish_needed FROM summarize_jobs WHERE session_id='s1'"
        ).fetchone() == (False,)
    finally:
        con.close()
