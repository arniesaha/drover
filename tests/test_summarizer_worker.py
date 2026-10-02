"""Tests for SummarizerWorker — drains summarize_session ledger jobs into session memory.

Jobs and derived rows live in the PostgreSQL control store (#480); the
session events the worker reads stay in the analytical DuckDB, bootstrapped at
the same registration path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from drover.schema import bootstrap
from drover.server.db import control_plane_connection
from drover.server.ledger import (
    BRIEF_PROJECT,
    EMBED_SESSION,
    SUMMARIZE_SESSION,
    JobLedger,
)
from drover.server.memory_store import MemoryRepository
from drover.server.summarizer.jobs import enqueue_summary_generation
from drover.server.summarizer.worker import (
    UNCONFIGURED_RELEASE_SECONDS,
    SummarizerWorker,
    _session_agent_events_ctes,
)


def _seed(tmp_path: Path, store_path: Path, *session_ids: str) -> Path:
    """Write events for ``session_ids`` and bootstrap the analytical DuckDB."""
    parquet_dir = tmp_path / "parquet"
    for session_id in session_ids:
        _write_events(parquet_dir, session_id)
    bootstrap(parquet_dir=parquet_dir, duckdb_path=store_path)
    return parquet_dir


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
            f"{session_id}-e1",
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
            f"{session_id}-e2",
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
    out = parquet_dir / "agent_events" / "date=2026-05-09" / "agent_id=macmini-claude"
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / f"part-{session_id}.parquet")


def _fake_llm_call(prompt: str, *, api_key, model, _client=None, **kw) -> dict:
    return {
        "summary_md": "Fixture summary describing the session.",
        "next_steps_md": "Move on to Plan 6.",
        "open_questions": ["use sse or streamable-http?"],
        "last_user_prompt": "do the thing",
        "last_assistant": "edited foo.py",
    }


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


class _FailingBackend(_StubBackend):
    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def summarize(self, prompt: str) -> dict:
        self.calls += 1
        raise RuntimeError(self.message)


def _jobs(store_path: Path, job_kind: str) -> list[tuple]:
    with control_plane_connection(store_path) as con:
        return con.execute(
            """SELECT subject_key, source_version, status, failures, payload_json
                 FROM pipeline_jobs WHERE job_kind = ?
                ORDER BY enqueued_at, subject_key""",
            [job_kind],
        ).fetchall()


def test_session_agent_events_cte_filters_before_canonical_dedupe() -> None:
    sql = _session_agent_events_ctes()

    assert "session_agent_events AS" in sql
    assert "FROM agent_events\n  WHERE session_id = ?" in sql
    assert "FROM session_agent_events ae" in sql


def test_success_writes_final_memory_and_fans_out_in_one_commit(
    tmp_path: Path, pg_control_path: Path
) -> None:
    _seed(tmp_path, pg_control_path, "sess-W1")
    assert enqueue_summary_generation(pg_control_path, "sess-W1", "v1") == "queued"

    worker = SummarizerWorker(
        duckdb_path=pg_control_path, api_key="sk-test", _llm_call=_fake_llm_call
    )
    assert worker.drain_once() == 1

    repo = MemoryRepository(pg_control_path)
    memory = repo.latest(["sess-W1"])["sess-W1"]
    assert memory.phase == "final"
    summary = memory.summary
    assert summary.summary_md == "Fixture summary describing the session."
    assert summary.next_steps_md == "Move on to Plan 6."
    assert summary.status == "completed"
    assert summary.source_version == "v1"
    assert summary.generator_model == worker.model
    assert summary.project_key == "arniesaha/nexus"
    assert summary.task_id == "tid1"
    assert summary.agent_id == "macmini-claude"
    assert summary.files_touched == ("src/foo.py",)
    assert summary.tools_used == {"Edit": 1}
    assert summary.open_questions == ("use sse or streamable-http?",)
    assert summary.ended_at is not None and summary.ended_at.tzinfo is not None

    ledger = JobLedger(pg_control_path)
    assert ledger.latest(SUMMARIZE_SESSION, "sess-W1").status == "succeeded"
    assert _jobs(pg_control_path, EMBED_SESSION) == [
        ("sess-W1", "v1", "pending", 0, "{}")
    ]
    ((subject, version, status, failures, payload),) = _jobs(
        pg_control_path, BRIEF_PROJECT
    )
    assert (subject, version, status, failures) == (
        "arniesaha/nexus",
        "sess-W1:v1",
        "pending",
        0,
    )
    assert json.loads(payload) == {
        "source_session_id": "sess-W1",
        "source_version": "v1",
    }
    # The same generation is not summarized twice.
    assert (
        enqueue_summary_generation(pg_control_path, "sess-W1", "v1") == "already_done"
    )


def test_downstream_enqueue_failure_rolls_the_summary_back(
    tmp_path: Path, pg_control_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Summary, completion and fan-out commit together or not at all."""
    _seed(tmp_path, pg_control_path, "sess-atomic")
    enqueue_summary_generation(pg_control_path, "sess-atomic", "v1")
    real_enqueue = JobLedger.enqueue

    def brief_enqueue_fails(self, job_kind, subject_key, **kwargs):
        if job_kind == BRIEF_PROJECT:
            raise RuntimeError("brief enqueue exploded")
        return real_enqueue(self, job_kind, subject_key, **kwargs)

    monkeypatch.setattr(JobLedger, "enqueue", brief_enqueue_fails)
    worker = SummarizerWorker(duckdb_path=pg_control_path, backend=_StubBackend())
    assert worker.drain_once() == 1

    assert MemoryRepository(pg_control_path).latest(["sess-atomic"]) == {}
    assert _jobs(pg_control_path, EMBED_SESSION) == []
    job = JobLedger(pg_control_path).latest(SUMMARIZE_SESSION, "sess-atomic")
    assert (job.status, job.failures) == ("retry_wait", 1)
    assert "brief enqueue exploded" in job.last_error


def test_superseded_generation_is_discarded_without_side_effects(
    tmp_path: Path, pg_control_path: Path
) -> None:
    _seed(tmp_path, pg_control_path, "s1")
    enqueue_summary_generation(pg_control_path, "s1", "v1")

    class SupersedingBackend(_StubBackend):
        def summarize(self, prompt: str) -> dict:
            # The session's next batch lands while the model runs.
            assert enqueue_summary_generation(pg_control_path, "s1", "v2") == "requeued"
            return super().summarize(prompt)

    backend = SupersedingBackend()
    worker = SummarizerWorker(duckdb_path=pg_control_path, backend=backend)
    assert worker.drain_once() == 1

    assert backend.calls == 1
    assert MemoryRepository(pg_control_path).latest(["s1"]) == {}
    assert _jobs(pg_control_path, EMBED_SESSION) == []
    assert _jobs(pg_control_path, BRIEF_PROJECT) == []
    jobs = _jobs(pg_control_path, SUMMARIZE_SESSION)
    assert [(v, s, f) for _, v, s, f, _ in jobs] == [
        ("v1", "superseded", 0),
        ("v2", "pending", 0),
    ]


def test_generation_change_at_success_seam_blocks_all_success_effects(
    tmp_path: Path, pg_control_path: Path
) -> None:
    _seed(tmp_path, pg_control_path, "s1")
    enqueue_summary_generation(pg_control_path, "s1", "v1")
    committed: list[str] = []

    worker = SummarizerWorker(
        duckdb_path=pg_control_path,
        backend=_StubBackend(),
        _before_success_effects=lambda: enqueue_summary_generation(
            pg_control_path, "s1", "v2"
        ),
        _after_completion_commit=lambda: committed.append("s1"),
    )
    assert worker.drain_once() == 1

    assert committed == []
    assert MemoryRepository(pg_control_path).summary("s1") is None
    assert _jobs(pg_control_path, EMBED_SESSION) == []
    latest = JobLedger(pg_control_path).latest(SUMMARIZE_SESSION, "s1")
    assert (latest.source_version, latest.status) == ("v2", "pending")


def test_retryable_failure_waits_for_retry(
    tmp_path: Path, pg_control_path: Path
) -> None:
    _seed(tmp_path, pg_control_path, "sess-W3")
    enqueue_summary_generation(pg_control_path, "sess-W3", "v1")

    def boom(prompt, **kw):
        raise RuntimeError("simulated network blip")

    worker = SummarizerWorker(
        duckdb_path=pg_control_path, api_key="sk-test", _llm_call=boom
    )
    assert worker.drain_once() == 1

    job = JobLedger(pg_control_path).latest(SUMMARIZE_SESSION, "sess-W3")
    assert (job.status, job.failures) == ("retry_wait", 1)
    assert "simulated network blip" in job.last_error
    assert job.next_run_at > datetime.now(timezone.utc)
    assert MemoryRepository(pg_control_path).summary("sess-W3") is None
    # Not due yet: the next tick has nothing to do.
    assert worker.drain_once() == 0


def test_validation_failure_is_quarantined_with_reason(
    tmp_path: Path, pg_control_path: Path
) -> None:
    _seed(tmp_path, pg_control_path, "sess-bad-json")
    enqueue_summary_generation(pg_control_path, "sess-bad-json", "v1")
    backend = _FailingBackend(
        "anthropic: LLM response missing required keys: next_steps_md"
    )

    worker = SummarizerWorker(duckdb_path=pg_control_path, backend=backend)
    assert worker.drain_once() == 1

    job = JobLedger(pg_control_path).latest(SUMMARIZE_SESSION, "sess-bad-json")
    assert job.status == "quarantined"
    assert job.error_category == "validation"
    assert "quarantined (validation)" in job.disposition_reason
    assert "missing required keys" in job.disposition_reason
    assert backend.calls == 1


def test_session_without_events_is_quarantined(
    tmp_path: Path, pg_control_path: Path
) -> None:
    _seed(tmp_path, pg_control_path)
    enqueue_summary_generation(pg_control_path, "sess-ghost", "v1")
    backend = _StubBackend()

    worker = SummarizerWorker(duckdb_path=pg_control_path, backend=backend)
    assert worker.drain_once() == 1

    job = JobLedger(pg_control_path).latest(SUMMARIZE_SESSION, "sess-ghost")
    assert (job.status, job.error_category) == ("quarantined", "no_events")
    assert "no events for session sess-ghost" in job.disposition_reason
    assert backend.calls == 0


def test_missing_api_key_releases_without_spending_an_attempt(
    tmp_path: Path, pg_control_path: Path
) -> None:
    _seed(tmp_path, pg_control_path, "sess-W2", "sess-W2b")
    enqueue_summary_generation(pg_control_path, "sess-W2", "v1")
    enqueue_summary_generation(pg_control_path, "sess-W2b", "v1")

    worker = SummarizerWorker(duckdb_path=pg_control_path, api_key=None, batch_size=5)
    # The batch stops at the first release: every job would hit the same wall.
    assert worker.drain_batch() == 1

    ledger = JobLedger(pg_control_path)
    released = [
        ledger.latest(SUMMARIZE_SESSION, sid) for sid in ("sess-W2", "sess-W2b")
    ]
    released = [job for job in released if job.status == "retry_wait"]
    assert len(released) == 1
    job = released[0]
    assert (job.failures, job.error_category) == (0, "released")
    assert "api_key" in job.last_error.lower()
    assert job.next_run_at > datetime.now(timezone.utc) + timedelta(
        seconds=UNCONFIGURED_RELEASE_SECONDS - 60
    )
    assert MemoryRepository(pg_control_path).counts()["session_summaries"] == 0


def test_failure_at_supersession_does_not_spend_the_new_budget(
    tmp_path: Path, pg_control_path: Path
) -> None:
    _seed(tmp_path, pg_control_path, "s1")
    enqueue_summary_generation(pg_control_path, "s1", "v1")

    worker = SummarizerWorker(
        duckdb_path=pg_control_path,
        backend=_FailingBackend("backend failed"),
        _before_failure_finish=lambda: enqueue_summary_generation(
            pg_control_path, "s1", "v2"
        ),
    )
    assert worker.drain_once() == 1

    jobs = _jobs(pg_control_path, SUMMARIZE_SESSION)
    assert [(v, s, f) for _, v, s, f, _ in jobs] == [
        ("v1", "superseded", 0),
        ("v2", "pending", 0),
    ]


def test_idle_drain_does_not_resolve_the_backend(
    tmp_path: Path, pg_control_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#55: no backend selection (and no fallback warning) on an empty queue."""
    _seed(tmp_path, pg_control_path)

    class ExplodingBackend:
        def ensure_ready(self):
            raise AssertionError("backend should not be warmed on an idle tick")

    worker = SummarizerWorker(duckdb_path=pg_control_path, backend=ExplodingBackend())
    monkeypatch.setattr(
        worker,
        "_resolve_backend",
        lambda: pytest.fail("backend resolved on an idle tick"),
    )
    assert worker.drain_once() == 0

    # A job that is waiting out its backoff is not due either.
    enqueue_summary_generation(pg_control_path, "later", "v1")
    with control_plane_connection(pg_control_path) as con:
        con.execute(
            "UPDATE pipeline_jobs SET status = 'retry_wait', "
            "next_run_at = now() + interval '1 hour'"
        )
    assert worker.drain_once() == 0


def test_drain_batch_processes_multiple_jobs_with_one_warmup(
    tmp_path: Path, pg_control_path: Path
) -> None:
    _seed(tmp_path, pg_control_path, "sess-B2", "sess-B3", "sess-B4")
    for sid in ("sess-B2", "sess-B3", "sess-B4"):
        enqueue_summary_generation(pg_control_path, sid, "v1")

    backend = _StubBackend()
    worker = SummarizerWorker(
        duckdb_path=pg_control_path, backend=backend, batch_size=10
    )
    assert worker.drain_batch() == 3
    assert backend.calls == 3
    # ensure_ready fires once per drain_batch call, not per job
    assert backend.ensure_calls == 1
    summaries = MemoryRepository(pg_control_path).summaries(
        ["sess-B2", "sess-B3", "sess-B4"]
    )
    assert {s.generator_model for s in summaries.values()} == {"stub-model-v1"}
    # The queue is empty: drain_batch stops instead of looping past it.
    assert worker.drain_batch() == 0
    assert backend.calls == 3


def test_memory_unavailable_is_a_quiet_no_op(tmp_path: Path) -> None:
    """A DuckDB control store has no ledger: enqueue and drain must not crash."""
    duckdb_path = tmp_path / "drover.duckdb"
    _seed(tmp_path, duckdb_path, "sess-duck")

    assert enqueue_summary_generation(duckdb_path, "sess-duck", "v1") == "unavailable"

    class ExplodingBackend:
        def ensure_ready(self):
            raise AssertionError("no backend work without a ledger")

        def summarize(self, prompt):
            raise AssertionError("no backend work without a ledger")

    worker = SummarizerWorker(duckdb_path=duckdb_path, backend=ExplodingBackend())
    assert worker.drain_once() == 0
    assert worker.drain_batch(max_jobs=5) == 0
