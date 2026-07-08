"""Tests for the new MCP tools: project_brief, recent_sessions, recall."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from drover.schema import bootstrap
from drover.server.mcp.tools import (
    drover_project_brief,
    drover_project_activity,
    drover_recall,
    drover_recent_sessions,
)


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _write_agent_events(
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
            "a",
            "tX",
            now,
            "user_message",
            "user",
            "hi",
            repo_owner,
            repo_name,
            "main",
            "arnab",
            f"{session_id}-k",
            "{}",
        )
    ]
    table = pa.table(
        {
            f.name: pa.array([r[i] for r in rows], type=f.type)
            for i, f in enumerate(schema)
        },
        schema=schema,
    )
    out = parquet_dir / "agent_events" / f"date={now.date()}" / "agent_id=a"
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / f"part-{session_id}.parquet")


def _write_span(
    parquet_dir: Path,
    *,
    span_id: str,
    agent_id: str,
    start_time: datetime,
    project: str | None = None,
) -> None:
    schema = pa.schema(
        [
            ("trace_id", pa.string()),
            ("span_id", pa.string()),
            ("parent_span_id", pa.string()),
            ("name", pa.string()),
            ("service_name", pa.string()),
            ("start_time", pa.timestamp("us", tz="UTC")),
            ("end_time", pa.timestamp("us", tz="UTC")),
            ("duration_ms", pa.float64()),
            ("session_id", pa.string()),
            ("task_id", pa.string()),
            ("agent_id", pa.string()),
            ("project", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("cost_usd", pa.float64()),
            ("dedup_key", pa.string()),
        ]
    )
    row = {
        "trace_id": f"trace-{span_id}",
        "span_id": span_id,
        "parent_span_id": None,
        "name": "llm_call",
        "service_name": "agentweave-proxy",
        "start_time": start_time,
        "end_time": start_time + timedelta(seconds=1),
        "duration_ms": 1000.0,
        "session_id": f"aw-{span_id}",
        "task_id": f"task-{span_id}",
        "agent_id": agent_id,
        "project": project,
        "repo_owner": None,
        "repo_name": None,
        "branch": None,
        "cost_usd": 1.25,
        "dedup_key": f"span-{span_id}",
    }
    table = pa.table(
        {field.name: pa.array([row[field.name]], type=field.type) for field in schema},
        schema=schema,
    )
    out = parquet_dir / "spans" / f"date={start_time.date().isoformat()}"
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / f"{span_id}.parquet")


def _insert_brief(duckdb_path: Path) -> None:
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("""INSERT INTO project_briefs
               (project_key, repo_owner, repo_name, brief_md, recent_themes_md,
                key_files, open_questions, next_steps_md, session_count,
                last_activity_at, generator_model, generated_at)
               VALUES ('arniesaha/nexus', 'arniesaha', 'nexus',
                       'Nexus is the local lakehouse.',
                       'Recent: hybrid summarization.',
                       ['src/nexus/server/wol.py'], ['which embed model?'],
                       'Land embeddings worker.', 5, now(), 'test-v1', now())""")
    finally:
        con.close()


def _insert_summary(
    duckdb_path: Path, session_id: str, *, ended_minutes_ago: int = 0
) -> None:
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            """INSERT INTO session_summaries
               (session_id, task_id, agent_id, ended_at, summary_md,
                files_touched, tools_used, last_user_prompt, last_assistant,
                next_steps_md, open_questions, status, generator_model, generated_at)
               VALUES (?, NULL, 'a', now() - INTERVAL (?) MINUTE, ?,
                       [], MAP{}, '', '', '', [], 'completed', 't', now())""",
            [session_id, ended_minutes_ago, f"summary {session_id}"],
        )
    finally:
        con.close()


def _insert_embedding(duckdb_path: Path, session_id: str, vector: list[float]) -> None:
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            """INSERT INTO session_embeddings (session_id, embedding, model, dim, embedded_at)
               VALUES (?, ?, 'test-embed', ?, now())""",
            [session_id, vector, len(vector)],
        )
    finally:
        con.close()


def test_project_brief_returns_row(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    _insert_brief(duckdb_path)
    out = drover_project_brief(
        duckdb_path=duckdb_path, repo_owner="arniesaha", repo_name="nexus"
    )
    assert out is not None
    assert out["brief_md"] == "Nexus is the local lakehouse."
    assert "src/nexus/server/wol.py" in out["key_files"]


def test_project_brief_marks_stale_when_newer_session_activity_exists(
    tmp_path: Path,
) -> None:
    _, duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            """INSERT INTO project_briefs
               (project_key, repo_owner, repo_name, brief_md, recent_themes_md,
                key_files, open_questions, next_steps_md, session_count,
                last_activity_at, generator_model, generated_at)
               VALUES ('arniesaha/nexus', 'arniesaha', 'nexus',
                       'Old marketplace-era brief.', 'old themes', [], [], '',
                       1, now() - INTERVAL 40 DAY, 'test-v1', now() - INTERVAL 40 DAY)"""
        )
        con.execute("""INSERT INTO session_summaries
               (session_id, task_id, agent_id, ended_at, summary_md,
                files_touched, tools_used, last_user_prompt, last_assistant,
                next_steps_md, open_questions, status, generator_model, generated_at)
               VALUES ('new-session', NULL, 'a', now(), 'new observatory work',
                       [], MAP{}, '', '', '', [], 'completed', 't', now())""")
        con.execute("""INSERT INTO tasks
               (task_id, repo_owner, repo_name, branch, principal_id, status,
                created_at, last_activity_at, session_count, total_cost_usd)
               VALUES ('task-new', 'arniesaha', 'nexus', 'main', 'arnab', 'open',
                       now(), now(), 1, 0.0)""")
        con.execute(
            "UPDATE session_summaries SET task_id='task-new' WHERE session_id='new-session'"
        )
    finally:
        con.close()

    out = drover_project_brief(
        duckdb_path=duckdb_path, repo_owner="arniesaha", repo_name="nexus"
    )

    assert out is not None
    assert out["stale"] is True
    assert out["freshness_status"] == "stale"
    assert "newer session activity" in out["freshness_warning"]
    assert out["latest_session_activity_at"] is not None


def test_project_brief_returns_none_for_unknown(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    out = drover_project_brief(duckdb_path=duckdb_path, project_key="ghost/repo")
    assert out is None


def test_project_brief_requires_identifier(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    with pytest.raises(ValueError, match="project_key or"):
        drover_project_brief(duckdb_path=duckdb_path)


def test_recent_sessions_returns_recent_first(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_agent_events(parquet_dir, session_id="S-old", repo_owner="o", repo_name="r")
    _write_agent_events(parquet_dir, session_id="S-new", repo_owner="o", repo_name="r")
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _insert_summary(duckdb_path, "S-old", ended_minutes_ago=60)
    _insert_summary(duckdb_path, "S-new", ended_minutes_ago=1)

    out = drover_recent_sessions(
        duckdb_path=duckdb_path, repo_owner="o", repo_name="r", limit=5
    )
    assert [s["session_id"] for s in out["sessions"]] == ["S-new", "S-old"]


def test_recent_sessions_quarantines_unknown_openclaw_summary(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_agent_events(
        parquet_dir,
        session_id="b58fbd05-native-openclaw",
        repo_owner="arniesaha",
        repo_name="openclaw",
    )
    _write_agent_events(
        parquet_dir,
        session_id="unknown_openclaw",
        repo_owner="arniesaha",
        repo_name="openclaw",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _insert_summary(duckdb_path, "unknown_openclaw", ended_minutes_ago=1)
    _insert_summary(duckdb_path, "b58fbd05-native-openclaw", ended_minutes_ago=5)

    out = drover_recent_sessions(
        duckdb_path=duckdb_path,
        repo_owner="arniesaha",
        repo_name="openclaw",
        limit=5,
    )

    assert [s["session_id"] for s in out["sessions"]] == ["b58fbd05-native-openclaw"]


def test_recent_sessions_respects_limit(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    for i in range(4):
        _write_agent_events(
            parquet_dir, session_id=f"S{i}", repo_owner="o", repo_name="r"
        )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    for i in range(4):
        _insert_summary(duckdb_path, f"S{i}", ended_minutes_ago=10 - i)
    out = drover_recent_sessions(duckdb_path=duckdb_path, project_key="o/r", limit=2)
    assert len(out["sessions"]) == 2


def test_project_activity_uses_enriched_span_attribution(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    now = datetime.now(timezone.utc)
    _write_agent_events(
        parquet_dir,
        session_id="session-with-repo",
        repo_owner="arniesaha",
        repo_name="nexus",
    )
    _write_span(
        parquet_dir,
        span_id="span-without-repo",
        agent_id="a",
        start_time=now,
        project="fallback-label",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    out = drover_project_activity(
        duckdb_path=duckdb_path,
        project_key="arniesaha/nexus",
        since=(now - timedelta(minutes=1)).isoformat(),
    )

    assert len(out["rows"]) == 1
    assert out["rows"][0]["project_key"] == "arniesaha/nexus"
    assert out["rows"][0]["agentweave_project"] == "fallback-label"


def test_project_activity_uses_bounded_enriched_span_partitions(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    now = datetime.now(timezone.utc)
    _write_agent_events(
        parquet_dir,
        session_id="session-with-repo",
        repo_owner="arniesaha",
        repo_name="nexus",
    )
    _write_span(
        parquet_dir,
        span_id="span-without-repo",
        agent_id="a",
        start_time=now,
        project="fallback-label",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    bad_agent_events = (
        parquet_dir
        / "agent_events"
        / "date=2026-01-01"
        / "agent_id=old-agent"
        / "part-bad.parquet"
    )
    bad_agent_events.parent.mkdir(parents=True, exist_ok=True)
    bad_agent_events.write_text("not parquet")

    out = drover_project_activity(
        duckdb_path=duckdb_path,
        project_key="arniesaha/nexus",
        since=(now - timedelta(minutes=1)).isoformat(),
    )

    assert len(out["rows"]) == 1
    assert out["rows"][0]["project_key"] == "arniesaha/nexus"


def test_recall_orders_by_cosine_similarity(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _insert_summary(duckdb_path, "near")
    _insert_summary(duckdb_path, "far")
    # query: [1, 0]; near is close, far is orthogonal
    _insert_embedding(duckdb_path, "near", [0.99, 0.01])
    _insert_embedding(duckdb_path, "far", [0.0, 1.0])

    out = drover_recall(duckdb_path=duckdb_path, query_embedding=[1.0, 0.0], limit=2)
    ids = [r["session_id"] for r in out["results"]]
    assert ids == ["near", "far"]
    assert [r["source_type"] for r in out["results"]] == [
        "session_summary",
        "session_summary",
    ]
    assert out["results"][0]["score"] > out["results"][1]["score"]


def test_recall_can_return_span_hits_distinct_from_summary_hits(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "summary-near")
    _insert_embedding(duckdb_path, "summary-near", [0.8, 0.2])
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("""INSERT INTO span_embeddings
               (span_id, trace_id, session_id, task_id, agent_id, repo_owner,
                repo_name, branch, source_text, source_fields, embedding, model,
                dim, embedded_at)
               VALUES ('span-near', 'trace-1', 'span-session', 'task', 'agent',
                       NULL, NULL, NULL, 'prompt: vector search bug',
                       ['prompt_preview'], [1.0, 0.0], 'test-embed', 2, now())""")
    finally:
        con.close()

    out = drover_recall(duckdb_path=duckdb_path, query_embedding=[1.0, 0.0], limit=2)

    assert [r["source_type"] for r in out["results"]] == ["span", "session_summary"]
    assert out["results"][0]["span_id"] == "span-near"
    assert out["results"][0]["source_text"] == "prompt: vector search bug"


def test_recall_filters_span_hits_by_persisted_span_repo(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("""INSERT INTO span_embeddings
               (span_id, trace_id, session_id, task_id, agent_id, repo_owner,
                repo_name, branch, source_text, source_fields, embedding, model,
                dim, embedded_at)
               VALUES
               ('span-nexus', 'trace-1', 'agentweave-session', 'task', 'agent',
                'arniesaha', 'nexus', 'main', 'prompt: nexus trace recall',
                ['prompt_preview'], [1.0, 0.0], 'test-embed', 2, now()),
               ('span-other', 'trace-2', 'agentweave-session-2', 'task', 'agent',
                'arniesaha', 'other', 'main', 'prompt: unrelated',
                ['prompt_preview'], [1.0, 0.0], 'test-embed', 2, now())""")
    finally:
        con.close()

    out = drover_recall(
        duckdb_path=duckdb_path,
        query_embedding=[1.0, 0.0],
        repo_owner="arniesaha",
        repo_name="nexus",
        limit=5,
    )

    assert [r["span_id"] for r in out["results"]] == ["span-nexus"]


def test_recall_ignores_embeddings_with_different_dimensions(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _insert_summary(duckdb_path, "same-dim")
    _insert_summary(duckdb_path, "other-provider-dim")
    _insert_embedding(duckdb_path, "same-dim", [0.99, 0.01])
    _insert_embedding(duckdb_path, "other-provider-dim", [1.0, 0.0, 0.0])

    out = drover_recall(duckdb_path=duckdb_path, query_embedding=[1.0, 0.0], limit=5)

    assert [r["session_id"] for r in out["results"]] == ["same-dim"]


def test_recall_requires_embedding(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    with pytest.raises(ValueError, match="required"):
        drover_recall(duckdb_path=duckdb_path)
