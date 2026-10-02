"""Seed derived memory (PostgreSQL control store) for reader tests (#480).

Summaries, briefs and embeddings are no longer DuckDB tables; tests that used
to INSERT into ``session_summaries``/``project_briefs`` write through the
repository instead. ``store_path`` is the ``pg_control_path`` fixture.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from drover.server.ledger import JobLedger
from drover.server.memory_store import (
    EmbeddingStore,
    MemoryRepository,
    ProjectBrief,
    SessionSummary,
)


def drive_job(
    store_path: Path,
    kind: str,
    subject: str,
    outcome: str = "pending",
    *,
    error: str = "boom",
) -> None:
    """One ledger job driven to ``outcome``.

    ``outcome`` is pending, running, succeeded, retry_wait or quarantined.
    """
    ledger = JobLedger(store_path, jitter=lambda low, high: 0.0)
    # Claimed work outranks anything already waiting, so the claim below
    # takes exactly this job.
    priority = 0 if outcome == "pending" else 1000
    ledger.enqueue(kind, subject, source_version="v1", priority=priority)
    if outcome == "pending":
        return
    [job] = ledger.claim(kind, worker_id="test")
    assert job.subject_key == subject
    if outcome == "succeeded":
        assert ledger.complete(job)
    elif outcome == "retry_wait":
        assert ledger.fail(job, error, retryable=True) == "retry_wait"
    elif outcome == "quarantined":
        assert ledger.fail(job, error, retryable=False) == "quarantined"
    elif outcome != "running":
        raise ValueError(outcome)


def put_embedding(store_path: Path, session_id: str, model: str = "m") -> None:
    """A 768-d vector for ``session_id`` (needs pgvector)."""
    vectors = EmbeddingStore(store_path, model=model)
    with vectors.connection() as con:
        vectors.put(con, session_id, [0.1, 0.2, *([0.0] * 766)], model=model)


def put_summary(store_path: Path, session_id: str, **fields: Any) -> SessionSummary:
    fields.setdefault("summary_md", f"summary of {session_id}")
    fields.setdefault("generated_at", datetime.now(timezone.utc))
    summary = SessionSummary(session_id=session_id, **fields)
    repo = MemoryRepository(store_path)
    with repo.connection() as con:
        repo.put_summary(con, summary)
    return summary


def put_brief(store_path: Path, project_key: str, **fields: Any) -> ProjectBrief:
    owner, _, name = project_key.partition("/")
    fields.setdefault("repo_owner", owner)
    fields.setdefault("repo_name", name)
    fields.setdefault("brief_md", f"brief of {project_key}")
    brief = ProjectBrief(project_key=project_key, **fields)
    repo = MemoryRepository(store_path)
    with repo.connection() as con:
        repo.put_brief(con, brief)
    return brief
