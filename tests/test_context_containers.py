"""Tests for confidence-aware context containers beyond repo attribution."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest
from memory_helpers import put_summary

from drover.context_containers import normalize_context_type
from drover.schema import bootstrap
from drover.server.mcp.tools import (
    drover_context_brief,
    drover_open_loops,
    drover_recent_contexts,
    drover_resume_context,
)


def _seed_context_db(tmp_path: Path, *, with_summary: bool = False) -> Path:
    """``with_summary`` writes the linked session summary to the PostgreSQL
    memory store; the caller must request ``pg_control_path``."""
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    if with_summary:
        put_summary(
            duckdb_path,
            "non-code-s1",
            agent_id="hermes",
            ended_at=datetime.now(timezone.utc),
            summary_md=(
                "Talked through a weekly planning conversation and captured "
                "follow-up loops."
            ),
            last_user_prompt="Help me plan the week",
            last_assistant="Captured priorities.",
            next_steps_md="Confirm the childcare calendar.",
            open_questions=("Which appointment moved?",),
            status="complete",
            generator_model="test",
        )
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("""INSERT INTO context_containers
                (context_id, container_type, label, source_harness, confidence,
                 evidence, last_touched_at, next_action, open_loop, session_ids,
                 task_ids, repo_owner, repo_name, branch, summary_md, redaction_policy)
                VALUES
                ('ctx-week-plan', 'open_floor_conversation', 'weekly planning',
                 'hermes', 0.86, 'non-code planning prompts; no repo cwd present',
                 now(), 'Confirm the childcare calendar.', 'appointment date unresolved',
                 ['non-code-s1'], [], NULL, NULL, NULL,
                 'Weekly planning discussion with a calendar follow-up.',
                 'session-summary-redacted'),
                ('ctx-home-shell', 'general_activity', 'home shell activity',
                 'openclaw', 0.72, 'cwd=/home/Arnab intentionally broad home workspace',
                 now() - INTERVAL 1 HOUR, NULL, NULL,
                 [], [], NULL, NULL, NULL,
                 'General host-level activity, not a failed repo attribution.',
                 'metadata-only')""")
    finally:
        con.close()
    return duckdb_path


def test_bootstrap_creates_context_containers_table(tmp_path: Path) -> None:
    duckdb_path = _seed_context_db(tmp_path)

    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name='context_containers'"
        ).fetchone()
    finally:
        con.close()

    assert row == (1,)


def test_non_code_context_is_queryable_and_resumable(
    tmp_path: Path, pg_control_path: Path
) -> None:
    duckdb_path = _seed_context_db(tmp_path, with_summary=True)

    recent = drover_recent_contexts(
        duckdb_path=duckdb_path, container_type="open_floor_conversation"
    )
    assert [ctx["context_id"] for ctx in recent["contexts"]] == ["ctx-week-plan"]
    assert recent["contexts"][0]["repo_owner"] is None
    assert recent["contexts"][0]["confidence"] == 0.86

    brief = drover_context_brief(duckdb_path=duckdb_path, label="weekly planning")
    assert brief is not None
    assert brief["container_type"] == "open_floor_conversation"
    assert "non-code" in brief["evidence"]
    assert brief["redaction_policy"] == "session-summary-redacted"

    loops = drover_open_loops(duckdb_path=duckdb_path)
    assert [ctx["context_id"] for ctx in loops["open_loops"]] == ["ctx-week-plan"]

    resume = drover_resume_context(duckdb_path=duckdb_path, context_id="ctx-week-plan")
    assert resume is not None
    assert resume["context"]["next_action"] == "Confirm the childcare calendar."
    assert [s["session_id"] for s in resume["session_summaries"]] == ["non-code-s1"]
    assert "weekly planning" in resume["session_summaries"][0]["summary_md"]


def test_general_activity_is_explicit_context_not_repo_failure(tmp_path: Path) -> None:
    duckdb_path = _seed_context_db(tmp_path)

    out = drover_recent_contexts(
        duckdb_path=duckdb_path, container_type="general_activity"
    )

    assert out["contexts"][0]["context_id"] == "ctx-home-shell"
    assert out["contexts"][0]["repo_owner"] is None
    assert "not a failed repo attribution" in out["contexts"][0]["summary_md"]


def test_context_type_validation_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="unsupported context container type"):
        normalize_context_type("unknown")
