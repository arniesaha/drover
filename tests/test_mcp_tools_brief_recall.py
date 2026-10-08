"""Tests for the new MCP tools: project_brief, recent_sessions, recall."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import pgvector_available
from memory_helpers import put_brief, put_summary

from drover.schema import bootstrap
from drover.server.db import control_plane_connection
from drover.server.mcp.tools import (
    drover_open_loops,
    drover_project_activity,
    drover_project_brief,
    drover_recall,
    drover_recent_sessions,
)
from drover.server.memory_store import EmbeddingMismatch, EmbeddingStore

EMBED_MODEL = "test-embed"


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    # `pg_control_path` registers PostgreSQL for this same path.
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _vec(*head: float) -> list[float]:
    """A 768-dimension vector (the session_embeddings space) from its head."""
    return [*head, *([0.0] * (768 - len(head)))]


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
    put_brief(
        duckdb_path,
        "arniesaha/nexus",
        brief_md="Nexus is the local lakehouse.",
        recent_themes_md="Recent: hybrid summarization.",
        key_files=("src/nexus/server/wol.py",),
        open_questions=("which embed model?",),
        next_steps_md="Land embeddings worker.",
        session_count=5,
        last_activity_at=datetime.now(timezone.utc),
        generator_model="test-v1",
    )


def _insert_summary(
    duckdb_path: Path,
    session_id: str,
    *,
    ended_minutes_ago: int = 0,
    summary_md: str | None = None,
    project_key: str | None = None,
) -> None:
    put_summary(
        duckdb_path,
        session_id,
        agent_id="a",
        project_key=project_key,
        ended_at=datetime.now(timezone.utc) - timedelta(minutes=ended_minutes_ago),
        summary_md=summary_md or f"summary {session_id}",
        generator_model="t",
    )


def _insert_embedding(duckdb_path: Path, session_id: str, vector: list[float]) -> None:
    store = EmbeddingStore(duckdb_path, model=EMBED_MODEL)
    with store.connection() as con:
        store.put(con, session_id, vector, model=EMBED_MODEL)


@pytest.fixture
def pgvector(postgres_dsn):
    if not pgvector_available(postgres_dsn):
        pytest.skip("pgvector is not installed on the test PostgreSQL server")


def test_project_brief_returns_row(tmp_path: Path, pg_control_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    _insert_brief(duckdb_path)
    out = drover_project_brief(
        duckdb_path=duckdb_path, repo_owner="arniesaha", repo_name="nexus"
    )
    assert out is not None
    assert out["brief_md"] == "Nexus is the local lakehouse."
    assert "src/nexus/server/wol.py" in out["key_files"]
    assert out["session_count"] == 5
    assert out["stale"] is False


def test_project_brief_marks_stale_when_newer_session_activity_exists(
    tmp_path: Path, pg_control_path: Path
) -> None:
    _, duckdb_path = _seed(tmp_path)
    forty_days_ago = datetime.now(timezone.utc) - timedelta(days=40)
    put_brief(
        duckdb_path,
        "arniesaha/nexus",
        brief_md="Old marketplace-era brief.",
        recent_themes_md="old themes",
        session_count=1,
        last_activity_at=forty_days_ago,
        generator_model="test-v1",
        generated_at=forty_days_ago,
    )
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("""INSERT INTO tasks
               (task_id, repo_owner, repo_name, branch, principal_id, status,
                created_at, last_activity_at, session_count, total_cost_usd)
               VALUES ('task-new', 'arniesaha', 'nexus', 'main', 'arnab', 'open',
                       now() - INTERVAL 50 DAY, now() - INTERVAL 50 DAY, 1, 0.0)""")
    finally:
        con.close()
    # Newer than the brief, linked to the repo only through its task.
    put_summary(
        duckdb_path,
        "new-session",
        task_id="task-new",
        agent_id="a",
        ended_at=datetime.now(timezone.utc),
        summary_md="new observatory work",
    )

    out = drover_project_brief(
        duckdb_path=duckdb_path, repo_owner="arniesaha", repo_name="nexus"
    )

    assert out is not None
    assert out["stale"] is True
    assert out["freshness_status"] == "stale"
    assert "newer session activity" in out["freshness_warning"]
    assert out["latest_session_activity_at"] is not None


def test_project_brief_returns_none_for_unknown(
    tmp_path: Path, pg_control_path: Path
) -> None:
    _, duckdb_path = _seed(tmp_path)
    out = drover_project_brief(duckdb_path=duckdb_path, project_key="ghost/repo")
    assert out is None


def test_project_brief_requires_identifier(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    with pytest.raises(ValueError, match="project_key or"):
        drover_project_brief(duckdb_path=duckdb_path)


def test_recent_sessions_returns_recent_first(
    tmp_path: Path, pg_control_path: Path
) -> None:
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


def test_recent_sessions_quarantines_unknown_openclaw_summary(
    tmp_path: Path, pg_control_path: Path
) -> None:
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


def test_recent_sessions_prefers_session_memory_project_key(
    tmp_path: Path, pg_control_path: Path
) -> None:
    """project_key alone attributes a summary: no tasks row, no day summary."""
    _, duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "S-keyed", project_key="o/r")
    _insert_summary(duckdb_path, "S-elsewhere", project_key="o/other")
    out = drover_recent_sessions(duckdb_path=duckdb_path, project_key="o/r")
    assert [s["session_id"] for s in out["sessions"]] == ["S-keyed"]
    assert set(out["sessions"][0]) == {
        "session_id",
        "agent_id",
        "ended_at",
        "summary_md",
        "next_steps_md",
        "open_questions",
        "files_touched",
        "generator_model",
        "generated_at",
        "store",
        "store_authoritative",
        "host",
        "data_watermark",
    }


def test_recent_sessions_respects_limit(tmp_path: Path, pg_control_path: Path) -> None:
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


def _sessions(out: dict) -> dict[str, dict]:
    return {s["session_id"]: s for day in out["days"] for s in day["sessions"]}


def test_project_activity_builds_a_timeline_from_events_and_summaries(
    tmp_path: Path,
    pg_control_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_agent_events(
        parquet_dir, session_id="native-1", repo_owner="arniesaha", repo_name="nexus"
    )
    _write_agent_events(
        parquet_dir, session_id="elsewhere", repo_owner="arniesaha", repo_name="other"
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    _insert_summary(duckdb_path, "native-1")
    with control_plane_connection(duckdb_path) as con:
        con.execute("""UPDATE session_memory
                  SET next_steps_md = 'Ship the graph route.',
                      open_questions = ARRAY['Which cap for days?']
                WHERE session_id = 'native-1'""")

    out = drover_project_activity(
        duckdb_path=duckdb_path, project_key="arniesaha/nexus"
    )

    assert out["source"] == "drover_sessions"
    assert out["window"]["days"] == 7
    assert [p["project_key"] for p in out["projects"]] == ["arniesaha/nexus"]
    assert out["projects"][0]["session_count"] == 1
    session = _sessions(out)["native-1"]
    assert session["title"] == "summary native-1"
    assert session["state"] == "done"
    assert session["launched_by_drover"] is False
    kinds = {(item["kind"], item["text"]) for item in out["open_items"]}
    assert ("next_step", "Ship the graph route.") in kinds
    assert ("open_question", "Which cap for days?") in kinds
    assert "elsewhere" not in _sessions(out)


def test_project_activity_reports_launched_session_state_and_tokens(
    tmp_path: Path,
) -> None:
    from drover.server.db import control_plane_path
    from drover.server.harness.registry import HarnessRegistry

    parquet_dir, duckdb_path = _seed(tmp_path)
    registry = HarnessRegistry(duckdb_path)
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    registry.create_session(
        session_id="launched-1",
        host_id="mac-mini",
        harness="claude-code",
        command="claude",
        status="running",
        started_at=started,
        repo_owner="arniesaha",
        repo_name="drover",
        branch="drover/launched-1",
        parent_session_id="orchestrator-1",
    )
    registry.update_session_activity("launched-1", awaiting="input")
    con = duckdb.connect(str(control_plane_path(duckdb_path)))
    try:
        con.execute("""INSERT INTO session_usage
                 (session_id, host_id, harness, input_tokens, output_tokens,
                  cache_read_tokens, cache_write_tokens, reasoning_tokens,
                  turn_count, exact, source, source_seq, source_event_count,
                  observed_at)
               VALUES ('launched-1', 'mac-mini', 'claude-code', 100, 20, 30, 0,
                       NULL, 1, TRUE, 'harness_events', 1, 1, now())""")
    finally:
        con.close()

    out = drover_project_activity(duckdb_path=duckdb_path, days=3)

    session = _sessions(out)["launched-1"]
    assert session["state"] == "awaiting_input"
    assert session["launched_by_drover"] is True
    assert session["harness"] == "claude-code"
    assert session["total_tokens"] == 150
    assert session["refs"] == {
        "branch": "drover/launched-1",
        "parent_session_id": "orchestrator-1",
    }
    assert session["duration_seconds"] >= 3500
    project = out["projects"][0]
    assert project["project_key"] == "arniesaha/drover"
    assert project["total_tokens"] == 150
    assert 0.9 < project["active_hours"] < 1.1
    assert out["open_items"][0]["kind"] == "awaiting_input"


def test_project_activity_never_reads_spans(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_span(
        parquet_dir,
        span_id="span-only",
        agent_id="a",
        start_time=datetime.now(timezone.utc),
        project="span-only-project",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    # A corrupt span partition would fail any query that touched it.
    bad = parquet_dir / "spans" / "date=2026-09-30" / "bad.parquet"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not parquet")

    out = drover_project_activity(duckdb_path=duckdb_path)

    assert out["projects"] == []
    assert out["days"] == []


def test_project_activity_tolerates_corrupt_partitions_outside_the_window(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_agent_events(
        parquet_dir,
        session_id="session-with-repo",
        repo_owner="arniesaha",
        repo_name="nexus",
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
        since=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    )

    assert list(_sessions(out)) == ["session-with-repo"]
    assert out["window"]["days"] == 1


def test_project_activity_caps_and_validates_inputs(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    for index in range(4):
        _write_agent_events(
            parquet_dir, session_id=f"S{index}", repo_owner="o", repo_name="r"
        )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    out = drover_project_activity(duckdb_path=duckdb_path, days=365, limit=2)

    assert out["window"]["days"] == 30
    assert len(_sessions(out)) == 2
    assert out["truncation_details"]["sessions"] is True
    assert out["projects"][0]["session_count"] == 4
    with pytest.raises(ValueError, match="owner"):
        drover_project_activity(duckdb_path=duckdb_path, project_key="not-a-pair")


def test_open_loops_scopes_project_key_to_exact_repository(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("""INSERT INTO context_containers
                (context_id, container_type, label, source_harness, confidence,
                 evidence, last_touched_at, next_action, open_loop, session_ids,
                 task_ids, repo_owner, repo_name, branch, summary_md, redaction_policy)
                VALUES
                ('ctx-drover-loop', 'code_project', 'Drover loop', 'codex', 0.95,
                 'repo-backed context', now(), 'Run the recall tests.', 'query is open',
                 [], [], 'arniesaha', 'drover', 'main', 'Drover work.',
                 'session-summary-redacted'),
                ('ctx-other-loop', 'code_project', 'Other loop', 'codex', 0.95,
                 'repo-backed context', now() - INTERVAL 1 HOUR, 'Review the other repo.',
                 'query is open', [], [], 'arniesaha', 'other', 'main', 'Other work.',
                 'session-summary-redacted')""")
    finally:
        con.close()

    unscoped = drover_open_loops(duckdb_path=duckdb_path)
    scoped = drover_open_loops(duckdb_path=duckdb_path, project_key="arniesaha/drover")

    assert [row["context_id"] for row in unscoped["open_loops"]] == [
        "ctx-drover-loop",
        "ctx-other-loop",
    ]
    assert [row["context_id"] for row in scoped["open_loops"]] == ["ctx-drover-loop"]


def test_recall_orders_by_cosine_similarity(
    tmp_path: Path, pg_control_path: Path, pgvector
) -> None:
    _, duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "near")
    _insert_summary(duckdb_path, "far")
    # query: [1, 0, ...]; near is close, far is orthogonal
    _insert_embedding(duckdb_path, "near", _vec(0.99, 0.01))
    _insert_embedding(duckdb_path, "far", _vec(0.0, 1.0))

    out = drover_recall(
        duckdb_path=duckdb_path,
        query_embedding=_vec(1.0, 0.0),
        limit=2,
        embedding_model=EMBED_MODEL,
    )
    ids = [r["session_id"] for r in out["results"]]
    assert ids == ["near", "far"]
    assert out["mode"] == "semantic"
    assert [r["source_type"] for r in out["results"]] == [
        "session_summary",
        "session_summary",
    ]
    assert out["results"][0]["score"] > out["results"][1]["score"]
    assert out["results"][0]["summary_md"] == "summary near"


def test_recall_scopes_semantic_hits_to_a_repo(
    tmp_path: Path, pg_control_path: Path, pgvector
) -> None:
    _, duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "in-repo", project_key="arniesaha/nexus")
    _insert_summary(duckdb_path, "elsewhere", project_key="arniesaha/other")
    _insert_embedding(duckdb_path, "in-repo", _vec(0.5, 0.5))
    _insert_embedding(duckdb_path, "elsewhere", _vec(1.0, 0.0))

    out = drover_recall(
        duckdb_path=duckdb_path,
        query_embedding=_vec(1.0, 0.0),
        repo_owner="arniesaha",
        repo_name="nexus",
        limit=5,
        embedding_model=EMBED_MODEL,
    )

    assert [r["session_id"] for r in out["results"]] == ["in-repo"]


def test_recall_rejects_a_query_from_another_embedding_space(
    tmp_path: Path, pg_control_path: Path
) -> None:
    """A wrong-dimension query is an explicit error, never a silent empty result."""
    _, duckdb_path = _seed(tmp_path)
    with pytest.raises(EmbeddingMismatch):
        drover_recall(
            duckdb_path=duckdb_path,
            query_embedding=[1.0, 0.0, 0.0],
            embedding_model=EMBED_MODEL,
        )


def test_recall_falls_back_to_keywords_without_pgvector(
    tmp_path: Path, pg_control_path: Path, postgres_dsn: str
) -> None:
    if pgvector_available(postgres_dsn):
        pytest.skip(
            "pgvector is installed; the vector-unavailable path is not reachable"
        )
    _, duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "match", summary_md="fixed the vector search bug")
    _insert_summary(duckdb_path, "miss", summary_md="unrelated work")

    out = drover_recall(
        duckdb_path=duckdb_path,
        query_embedding=_vec(1.0),
        query="vector search",
        embedding_model=EMBED_MODEL,
    )

    assert out["mode"] == "keyword"
    assert "pgvector" in out["reason"]
    assert out["memory_unavailable"] is False
    assert [r["session_id"] for r in out["results"]] == ["match"]
    assert out["results"][0]["score"] is None


def test_recall_without_a_configured_model_uses_keywords(
    tmp_path: Path, pg_control_path: Path
) -> None:
    _, duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "match", summary_md="recall keyword fallback")
    out = drover_recall(
        duckdb_path=duckdb_path, query_embedding=_vec(1.0), query="keyword"
    )
    assert out["mode"] == "keyword"
    assert "embedding model" in out["reason"]
    assert [r["session_id"] for r in out["results"]] == ["match"]


def test_recall_requires_embedding(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    with pytest.raises(ValueError, match="required"):
        drover_recall(duckdb_path=duckdb_path)
