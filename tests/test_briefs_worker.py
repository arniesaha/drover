"""Tests for BriefWorker — drains brief_jobs into project_briefs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from drover.schema import bootstrap
from drover.server.briefs.worker import (
    BriefWorker,
    enqueue_brief,
    enqueue_briefs_for_active_projects,
)
from drover.server.jobs import JobStream
from drover.server.summarizer.backends import BackendReadinessError


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _write_session_events(
    parquet_dir: Path, *, session_id: str, repo_owner: str, repo_name: str
) -> None:
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
            f"{session_id}-1",
            session_id,
            "macmini",
            "task-X",
            now,
            "user_message",
            "user",
            "do thing",
            repo_owner,
            repo_name,
            "main",
            "arnab",
            "k",
            "{}",
        ),
    ]
    table = pa.table(
        {
            f.name: pa.array([r[i] for r in rows], type=f.type)
            for i, f in enumerate(schema)
        },
        schema=schema,
    )
    out = (
        parquet_dir
        / "agent_events"
        / f"date={now.date().isoformat()}"
        / "agent_id=macmini"
    )
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / f"part-{session_id}.parquet")


def _insert_summary(
    duckdb_path: Path, *, session_id: str, task_id: str = "task-X"
) -> None:
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            """INSERT INTO session_summaries
               (session_id, task_id, agent_id, ended_at, summary_md,
                files_touched, tools_used, last_user_prompt, last_assistant,
                next_steps_md, open_questions, status, generator_model, generated_at)
               VALUES (?, ?, 'macmini', now(), ?, ?, MAP{}, '', '', ?, ?, 'completed', 'test', now())""",
            [
                session_id,
                task_id,
                f"summary for {session_id}",
                ["src/foo.py", "src/bar.py"],
                "next: do more",
                ["how about q?"],
            ],
        )
    finally:
        con.close()


def _insert_task(
    duckdb_path: Path, *, task_id: str, repo_owner: str, repo_name: str
) -> None:
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            """INSERT INTO tasks (task_id, repo_owner, repo_name, branch, status, created_at, last_activity_at)
               VALUES (?, ?, ?, 'main', 'open', now(), now())""",
            [task_id, repo_owner, repo_name],
        )
    finally:
        con.close()


class _StubBackend:
    name = "stub"
    model = "stub-brief-v1"

    def summarize(self, prompt: str) -> dict:
        return {
            "brief_md": "Project synthesizes incident data via FastAPI on Railway.",
            "recent_themes_md": "Refactoring the database layer; testing migrations.",
            "key_files": ["app/models.py", "app/main.py"],
            "open_questions": ["which DB driver?"],
            "next_steps_md": "Add the missing migration.",
        }


class _ReadinessThenSuccessBackend(_StubBackend):
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.ready_checks = 0
        self.summarize_calls = 0

    def ensure_ready(self) -> None:
        self.ready_checks += 1
        if self.ready_checks <= self.failures:
            raise BackendReadinessError(
                "ollama readiness: local model 'qwen2.5:7b' cold-start warmup timed out after 120s"
            )

    def summarize(self, prompt: str) -> dict:
        self.summarize_calls += 1
        return super().summarize(prompt)


def test_enqueue_brief_inserts_pending_row(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    out = enqueue_brief(duckdb_path, "arniesaha/nexus")
    assert out == "queued"
    con = duckdb.connect(str(duckdb_path))
    try:
        row = con.execute(
            "SELECT status FROM brief_jobs WHERE project_key='arniesaha/nexus'"
        ).fetchone()
    finally:
        con.close()
    assert row[0] == "pending"


def test_enqueue_brief_requeues_done(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    enqueue_brief(duckdb_path, "x/y")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("UPDATE brief_jobs SET status='done' WHERE project_key='x/y'")
    finally:
        con.close()
    assert enqueue_brief(duckdb_path, "x/y") == "requeued"


def test_enqueue_brief_already_queued(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    enqueue_brief(duckdb_path, "x/y")
    assert enqueue_brief(duckdb_path, "x/y") == "already_queued"


def test_enqueue_briefs_for_active_projects_queues_attributed_projects(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    # Attributed task with a linked summary → should enqueue
    _insert_task(
        duckdb_path, task_id="task-A", repo_owner="arniesaha", repo_name="nexus"
    )
    _insert_summary(duckdb_path, session_id="S-A", task_id="task-A")
    # Attributed task with NO summary → should NOT enqueue
    _insert_task(
        duckdb_path, task_id="task-B", repo_owner="arniesaha", repo_name="empty"
    )
    # Unattributed task → should NOT enqueue
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("""INSERT INTO tasks (task_id, status, created_at, last_activity_at)
               VALUES ('task-C', 'open', now(), now())""")
    finally:
        con.close()

    results = enqueue_briefs_for_active_projects(duckdb_path)
    keys = {pk for pk, _ in results}
    assert keys == {"arniesaha/nexus"}


def test_enqueue_briefs_for_active_projects_respects_window(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    _insert_task(
        duckdb_path, task_id="task-old", repo_owner="arniesaha", repo_name="stale"
    )
    _insert_summary(duckdb_path, session_id="S-old", task_id="task-old")
    # Push activity well outside the window
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "UPDATE tasks SET last_activity_at = now() - INTERVAL 30 DAY WHERE task_id='task-old'"
        )
    finally:
        con.close()

    assert enqueue_briefs_for_active_projects(duckdb_path, hours=1) == []
    assert enqueue_briefs_for_active_projects(duckdb_path, hours=24 * 60) != []


def test_brief_worker_drains_one_job(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _insert_task(
        duckdb_path, task_id="task-X", repo_owner="arniesaha", repo_name="nexus"
    )
    _write_session_events(
        parquet_dir, session_id="S1", repo_owner="arniesaha", repo_name="nexus"
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _insert_summary(duckdb_path, session_id="S1")
    enqueue_brief(duckdb_path, "arniesaha/nexus")

    worker = BriefWorker(duckdb_path=duckdb_path, backend=_StubBackend())
    n = worker.drain_once()
    assert n == 1

    con = duckdb.connect(str(duckdb_path))
    try:
        row = con.execute(
            "SELECT brief_md, recent_themes_md, key_files, generator_model "
            "FROM project_briefs WHERE project_key='arniesaha/nexus'"
        ).fetchone()
        job = con.execute(
            "SELECT status FROM brief_jobs WHERE project_key='arniesaha/nexus'"
        ).fetchone()
    finally:
        con.close()
    assert "FastAPI" in row[0]
    assert "Refactoring" in row[1]
    assert row[2] == ["app/models.py", "app/main.py"]
    assert row[3] == "stub-brief-v1"
    assert job[0] == "done"


def test_brief_worker_leaves_local_readiness_timeout_retryable(
    tmp_path: Path,
) -> None:
    _, duckdb_path = _seed(tmp_path)
    _insert_task(
        duckdb_path, task_id="task-X", repo_owner="arniesaha", repo_name="nexus"
    )
    _insert_summary(duckdb_path, session_id="S1")
    enqueue_brief(duckdb_path, "arniesaha/nexus")
    backend = _ReadinessThenSuccessBackend(failures=1)

    worker = BriefWorker(duckdb_path=duckdb_path, backend=backend)
    assert worker.drain_once() == 1

    con = duckdb.connect(str(duckdb_path))
    try:
        status, attempts, err = con.execute(
            "SELECT status, attempts, last_error FROM brief_jobs WHERE project_key='arniesaha/nexus'"
        ).fetchone()
        brief = con.execute(
            "SELECT 1 FROM project_briefs WHERE project_key='arniesaha/nexus'"
        ).fetchone()
    finally:
        con.close()
    assert status == "pending"
    assert attempts == 0
    assert "retryable local model readiness failure" in err
    assert "cold-start warmup timed out" in err
    assert brief is None
    assert backend.summarize_calls == 0


def test_brief_worker_succeeds_after_transient_readiness_retry(
    tmp_path: Path,
) -> None:
    _, duckdb_path = _seed(tmp_path)
    _insert_task(
        duckdb_path, task_id="task-X", repo_owner="arniesaha", repo_name="nexus"
    )
    _insert_summary(duckdb_path, session_id="S1")
    enqueue_brief(duckdb_path, "arniesaha/nexus")
    backend = _ReadinessThenSuccessBackend(failures=1)

    worker = BriefWorker(duckdb_path=duckdb_path, backend=backend)
    assert worker.drain_once() == 1
    assert worker.drain_once() == 1

    con = duckdb.connect(str(duckdb_path))
    try:
        status, attempts, err = con.execute(
            "SELECT status, attempts, last_error FROM brief_jobs WHERE project_key='arniesaha/nexus'"
        ).fetchone()
        brief_md = con.execute(
            "SELECT brief_md FROM project_briefs WHERE project_key='arniesaha/nexus'"
        ).fetchone()[0]
    finally:
        con.close()
    assert status == "done"
    assert attempts == 1
    assert err is None
    assert "FastAPI" in brief_md
    assert backend.ready_checks == 2
    assert backend.summarize_calls == 1


def test_brief_worker_uses_task_stats_when_task_summaries_exist(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    _insert_task(
        duckdb_path, task_id="task-X", repo_owner="arniesaha", repo_name="nexus"
    )
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "UPDATE tasks SET session_count=7, last_activity_at=TIMESTAMP '2026-06-01 12:00:00' WHERE task_id='task-X'"
        )
    finally:
        con.close()
    _insert_summary(duckdb_path, session_id="S1")
    enqueue_brief(duckdb_path, "arniesaha/nexus")

    worker = BriefWorker(duckdb_path=duckdb_path, backend=_StubBackend())
    assert worker.drain_once() == 1

    con = duckdb.connect(str(duckdb_path))
    try:
        session_count, last_activity = con.execute(
            "SELECT session_count, last_activity_at FROM project_briefs WHERE project_key='arniesaha/nexus'"
        ).fetchone()
    finally:
        con.close()
    assert session_count == 7
    assert last_activity.isoformat() == "2026-06-01T12:00:00"


def test_brief_worker_marks_errored_when_no_summaries(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    enqueue_brief(duckdb_path, "ghost/repo")

    worker = BriefWorker(duckdb_path=duckdb_path, backend=_StubBackend())
    worker.drain_once()

    con = duckdb.connect(str(duckdb_path))
    try:
        status, err = con.execute(
            "SELECT status, last_error FROM brief_jobs WHERE project_key='ghost/repo'"
        ).fetchone()
    finally:
        con.close()
    assert status == "errored"
    assert "no session_summaries" in err


def test_brief_worker_drain_returns_zero_when_empty(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    worker = BriefWorker(duckdb_path=duckdb_path, backend=_StubBackend())
    assert worker.drain_once() == 0


def test_brief_worker_handles_malformed_project_key(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    enqueue_brief(duckdb_path, "no-slash-here")
    worker = BriefWorker(duckdb_path=duckdb_path, backend=_StubBackend())
    worker.drain_once()

    con = duckdb.connect(str(duckdb_path))
    try:
        status, err = con.execute(
            "SELECT status, last_error FROM brief_jobs WHERE project_key='no-slash-here'"
        ).fetchone()
    finally:
        con.close()
    assert status == "errored"
    assert "malformed" in err


def test_brief_worker_acks_stream_job_after_durable_write(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _insert_task(
        duckdb_path, task_id="task-X", repo_owner="arniesaha", repo_name="nexus"
    )
    _write_session_events(
        parquet_dir, session_id="S1", repo_owner="arniesaha", repo_name="nexus"
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _insert_summary(duckdb_path, session_id="S1")
    enqueue_brief(duckdb_path, "arniesaha/nexus")
    stream = JobStream("brief_jobs")
    stream.add({"project_key": "arniesaha/nexus"})

    worker = BriefWorker(
        duckdb_path=duckdb_path,
        backend=_StubBackend(),
        job_stream=stream,
        worker_id="brief-worker-a",
    )

    assert worker.drain_once() == 1
    assert stream.pending() == []
    assert stream.length() == 0


def test_brief_worker_leaves_failed_stream_job_unacked(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    enqueue_brief(duckdb_path, "ghost/repo")
    stream = JobStream("brief_jobs", visibility_timeout_ms=0)
    stream.add({"project_key": "ghost/repo"})

    worker = BriefWorker(
        duckdb_path=duckdb_path,
        backend=_StubBackend(),
        job_stream=stream,
        worker_id="brief-worker-a",
    )

    assert worker.drain_once() == 1
    pending = stream.pending()
    assert len(pending) == 1
    assert pending[0].last_error and "no session_summaries" in pending[0].last_error
    reclaimed = stream.reclaim("brief-worker-b")
    assert len(reclaimed) == 1
    assert reclaimed[0].fields["project_key"] == "ghost/repo"


def test_brief_worker_acks_redelivery_when_brief_already_done(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    enqueue_brief(duckdb_path, "arniesaha/nexus")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "UPDATE brief_jobs SET status='done' WHERE project_key='arniesaha/nexus'"
        )
    finally:
        con.close()
    stream = JobStream("brief_jobs", visibility_timeout_ms=0)
    stream.add({"project_key": "arniesaha/nexus"})

    worker = BriefWorker(
        duckdb_path=duckdb_path,
        backend=_StubBackend(),
        job_stream=stream,
        worker_id="brief-worker-b",
    )

    assert worker.drain_once() == 1
    assert stream.pending() == []
    assert stream.length() == 0


def test_brief_worker_acks_obsolete_versioned_job_without_backend_execution(
    tmp_path: Path,
) -> None:
    _, duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, source_version) "
            "VALUES ('S-stale', 'pending', 'v2')"
        )
        con.execute("""INSERT INTO brief_jobs
               (project_key, status, attempts, source_session_id, source_version)
               VALUES ('arniesaha/nexus', 'pending', 0, 'S-stale', 'v1')""")
    finally:
        con.close()
    stream = JobStream("brief_jobs")
    stream.add(
        {
            "project_key": "arniesaha/nexus",
            "source_session_id": "S-stale",
            "source_version": "v1",
        }
    )

    class CountingBackend(_StubBackend):
        def __init__(self) -> None:
            self.calls = 0

        def summarize(self, prompt: str) -> dict:
            self.calls += 1
            return super().summarize(prompt)

    backend = CountingBackend()
    worker = BriefWorker(
        duckdb_path=duckdb_path,
        backend=backend,
        job_stream=stream,
    )

    assert worker.drain_once() == 1
    assert backend.calls == 0
    assert stream.pending() == []
    assert stream.length() == 0
