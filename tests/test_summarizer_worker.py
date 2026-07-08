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


def test_worker_marks_errored_when_no_api_key(tmp_path: Path) -> None:
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
        assert status[0] == "errored"
        assert status[1] and "api_key" in status[1].lower()

        # No session_summaries row written
        n = con.execute("SELECT count(*) FROM session_summaries").fetchone()[0]
        assert n == 0
    finally:
        con.close()


def test_worker_handles_llm_failure_gracefully(tmp_path: Path) -> None:
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
        assert status[0] == "errored"
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
    stream = JobStream("summarize_jobs", visibility_timeout_ms=0)
    stream.add({"session_id": "sess-stream-fail"})

    worker = SummarizerWorker(
        duckdb_path=duckdb_path,
        api_key=None,
        job_stream=stream,
        worker_id="worker-a",
    )

    assert worker.drain_once() == 1
    pending = stream.pending()
    assert len(pending) == 1
    assert pending[0].last_error and "api_key" in pending[0].last_error.lower()
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
