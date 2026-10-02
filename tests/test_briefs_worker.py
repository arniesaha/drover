"""Tests for BriefWorker — drains brief_project ledger jobs into project_briefs (#480)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.briefs.worker import (
    BriefWorker,
    enqueue_brief,
    enqueue_briefs_for_active_projects,
)
from drover.server.db import control_plane_connection
from drover.server.ledger import BRIEF_PROJECT, JobLedger
from drover.server.memory_store import MemoryRepository, SessionSummary
from drover.server.summarizer.backends import BackendError, BackendReadinessError


@pytest.fixture
def store(pg_control_path: Path, tmp_path: Path) -> Path:
    """The PG control store plus the analytical DuckDB at the same path."""
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=pg_control_path)
    return pg_control_path


def _put_summary(
    path: Path,
    session_id: str,
    *,
    project_key: str | None = "arniesaha/nexus",
    ended_at: datetime | None = None,
) -> None:
    with control_plane_connection(path) as con:
        MemoryRepository.put_summary(
            con,
            SessionSummary(
                session_id=session_id,
                summary_md=f"summary for {session_id}",
                next_steps_md="next: do more",
                project_key=project_key,
                ended_at=ended_at or datetime.now(timezone.utc),
                files_touched=("src/foo.py", "src/bar.py"),
                open_questions=("how about q?",),
                source_version="v1",
            ),
        )


def _insert_task(
    path: Path, *, task_id: str = "task-X", session_count: int | None = None
) -> None:
    con = duckdb.connect(str(path))
    try:
        con.execute(
            """INSERT INTO tasks (task_id, repo_owner, repo_name, branch, status, created_at,
                                  last_activity_at, session_count)
               VALUES (?, 'arniesaha', 'nexus', 'main', 'open', now(),
                       TIMESTAMP '2026-06-01 12:00:00', ?)""",
            [task_id, session_count],
        )
    finally:
        con.close()


def _enqueue(path: Path, project_key: str = "arniesaha/nexus") -> None:
    JobLedger(path).enqueue(
        BRIEF_PROJECT,
        project_key,
        source_version="v1",
        payload={"source_session_id": "S1", "source_version": "v1"},
    )


def _job(path: Path, project_key: str = "arniesaha/nexus"):
    return JobLedger(path).latest(BRIEF_PROJECT, project_key)


def _make_due(path: Path) -> None:
    with control_plane_connection(path) as con:
        con.execute("UPDATE pipeline_jobs SET next_run_at = now()")


class _StubBackend:
    name = "stub"
    model = "stub-brief-v1"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def summarize(self, prompt: str) -> dict:
        self.prompts.append(prompt)
        return {
            "brief_md": "Project synthesizes incident data via FastAPI on Railway.",
            "recent_themes_md": "Refactoring the database layer; testing migrations.",
            "key_files": ["app/models.py", "app/main.py"],
            "open_questions": ["which DB driver?"],
            "next_steps_md": "Add the missing migration.",
        }


class _ReadinessThenSuccessBackend(_StubBackend):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.ready_checks = 0

    def ensure_ready(self) -> None:
        self.ready_checks += 1
        if self.ready_checks <= self.failures:
            raise BackendReadinessError(
                "ollama readiness: local model 'qwen2.5:7b' cold-start warmup timed out after 120s"
            )


class _FailingBackend(_StubBackend):
    def summarize(self, prompt: str) -> dict:
        raise BackendError("anthropic: 529 overloaded")


# --- enqueue -----------------------------------------------------------------


def test_enqueue_brief_queues_then_dedupes_then_requeues_after_success(
    store: Path,
) -> None:
    assert enqueue_brief(store, "x/y") == "queued"
    assert enqueue_brief(store, "x/y") == "already_queued"
    ledger = JobLedger(store)
    (job,) = ledger.claim(BRIEF_PROJECT, worker_id="t")
    assert enqueue_brief(store, "x/y") == "already_queued"  # running is live
    assert ledger.complete(job)
    # Briefs decay with activity: a succeeded project can always be re-run.
    assert enqueue_brief(store, "x/y") == "requeued"
    assert _job(store, "x/y").status == "pending"


def test_enqueue_brief_is_unavailable_without_postgres(tmp_path: Path) -> None:
    path = tmp_path / "drover.duckdb"
    assert enqueue_brief(path, "x/y") == "unavailable"
    assert enqueue_briefs_for_active_projects(path) == []


def test_enqueue_briefs_for_active_projects_uses_recent_final_summaries(
    store: Path,
) -> None:
    now = datetime.now(timezone.utc)
    _put_summary(store, "S-A", project_key="arniesaha/nexus")
    _put_summary(
        store, "S-old", project_key="arniesaha/stale", ended_at=now - timedelta(days=30)
    )
    _put_summary(store, "S-none", project_key=None)
    _put_summary(store, "S-bad", project_key="no-slash")
    with control_plane_connection(store) as con:
        MemoryRepository.put_recap(con, "S-live", "live only", 1, None)

    assert enqueue_briefs_for_active_projects(store) == [("arniesaha/nexus", "queued")]
    assert {
        pk for pk, _ in enqueue_briefs_for_active_projects(store, hours=24 * 60)
    } == {
        "arniesaha/nexus",
        "arniesaha/stale",
    }


# --- worker ------------------------------------------------------------------


def test_brief_worker_drain_returns_zero_when_empty(store: Path) -> None:
    worker = BriefWorker(duckdb_path=store, backend=_StubBackend())
    worker._resolve_backend = lambda: pytest.fail("idle drain resolved the backend")
    assert worker.drain_once() == 0


def test_brief_worker_without_postgres_memory_is_a_noop(tmp_path: Path) -> None:
    worker = BriefWorker(duckdb_path=tmp_path / "drover.duckdb", backend=_StubBackend())
    assert worker.drain_once() == 0


def test_brief_worker_writes_brief_and_completes(store: Path) -> None:
    _insert_task(store, session_count=7)
    _put_summary(store, "S1")
    _enqueue(store)
    backend = _StubBackend()

    worker = BriefWorker(duckdb_path=store, backend=backend)
    assert worker.drain_once() == 1

    brief = MemoryRepository(store).brief("arniesaha/nexus")
    assert brief is not None
    assert "FastAPI" in brief.brief_md
    assert "Refactoring" in brief.recent_themes_md
    assert brief.key_files == ("app/models.py", "app/main.py")
    assert brief.generator_model == "stub-brief-v1"
    assert brief.session_count == 7
    assert brief.last_activity_at.replace(tzinfo=None) == datetime(2026, 6, 1, 12, 0)
    assert (brief.source_session_id, brief.source_version) == ("S1", "v1")
    assert "summary for S1" in backend.prompts[0]
    assert _job(store).status == "succeeded"
    assert worker.drain_once() == 0


def test_brief_worker_falls_back_to_summary_stats_without_tasks(store: Path) -> None:
    _put_summary(store, "S1")
    _put_summary(store, "S2")
    _enqueue(store)

    assert BriefWorker(duckdb_path=store, backend=_StubBackend()).drain_once() == 1
    assert MemoryRepository(store).brief("arniesaha/nexus").session_count == 2


def test_brief_worker_releases_on_readiness_failure_then_succeeds(store: Path) -> None:
    _insert_task(store)
    _put_summary(store, "S1")
    _enqueue(store)
    backend = _ReadinessThenSuccessBackend(failures=1)
    worker = BriefWorker(duckdb_path=store, backend=backend)

    assert worker.drain_once() == 1
    job = _job(store)
    assert job.status == "retry_wait" and job.error_category == "released"
    assert job.failures == 0
    assert "retryable local model readiness failure" in job.last_error
    assert "cold-start warmup timed out" in job.last_error
    assert MemoryRepository(store).brief("arniesaha/nexus") is None
    assert backend.prompts == []

    assert worker.drain_once() == 0  # released with a delay, not hot-looped
    _make_due(store)
    assert worker.drain_once() == 1
    assert _job(store).status == "succeeded"
    assert backend.ready_checks == 2 and len(backend.prompts) == 1


def test_brief_worker_releases_without_backend(store: Path) -> None:
    _put_summary(store, "S1")
    _enqueue(store)
    assert BriefWorker(duckdb_path=store).drain_once() == 1
    job = _job(store)
    assert job.status == "retry_wait" and job.failures == 0
    assert "no backend configured" in job.last_error


def test_brief_worker_quarantines_when_no_summaries(store: Path) -> None:
    _enqueue(store, "ghost/repo")
    assert BriefWorker(duckdb_path=store, backend=_StubBackend()).drain_once() == 1
    job = _job(store, "ghost/repo")
    assert job.status == "quarantined" and job.error_category == "no_summaries"
    assert "no session summaries" in job.disposition_reason


def test_brief_worker_quarantines_malformed_project_key(store: Path) -> None:
    _enqueue(store, "no-slash-here")
    assert BriefWorker(duckdb_path=store, backend=_StubBackend()).drain_once() == 1
    job = _job(store, "no-slash-here")
    assert job.status == "quarantined" and "malformed" in job.disposition_reason


def test_brief_worker_retries_backend_failure(store: Path) -> None:
    _put_summary(store, "S1")
    _enqueue(store)
    assert BriefWorker(duckdb_path=store, backend=_FailingBackend()).drain_once() == 1
    job = _job(store)
    assert job.status == "retry_wait" and job.failures == 1
    assert job.error_category == "brief_backend" and "529" in job.last_error
    assert MemoryRepository(store).brief("arniesaha/nexus") is None
