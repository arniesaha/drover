from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner
from conftest import pgvector_available
from memory_helpers import put_brief, put_summary

from drover.schema import bootstrap
from drover.server.__main__ import main
from drover.server.memory_store import EmbeddingStore
from drover.server.observatory import pipeline_observatory_snapshot


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

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
        {
            "id": "evt-1",
            "session_id": "sess-1",
            "agent_id": "openclaw-main",
            "task_id": "task-1",
            "timestamp": now,
            "event_type": "user_message",
            "role": "user",
            "content": "show me the artifact",
            "repo_owner": "arniesaha",
            "repo_name": "nexus",
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "evt-1",
            "raw_data": "{}",
        }
    ]
    out = parquet_dir / "agent_events" / f"date={now.date()}" / "agent_id=openclaw-main"
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                field.name: pa.array([rows[0][field.name]], type=field.type)
                for field in schema
            },
            schema=schema,
        ),
        out / "part.parquet",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            """INSERT INTO tasks
               (task_id, repo_owner, repo_name, branch, status, title)
               VALUES ('task-1', 'arniesaha', 'nexus', 'main', 'active', 'Observatory')"""
        )
    finally:
        con.close()
    return parquet_dir, duckdb_path


def _seed_memory(duckdb_path: Path, *, embed: bool) -> None:
    """Derived memory for sess-1, in the PostgreSQL memory store (#480)."""
    put_summary(
        duckdb_path,
        "sess-1",
        task_id="task-1",
        agent_id="openclaw-main",
        project_key="arniesaha/nexus",
        ended_at=datetime.now(timezone.utc),
        summary_md="Built drilldown",
        files_touched=("src/drover/server/observatory.py",),
        tools_used={"pytest": 1},
        last_user_prompt="add artifacts",
        last_assistant="added the artifact surface",
        next_steps_md="deploy it",
        status="complete",
        generator_model="test-model",
    )
    put_brief(
        duckdb_path,
        "arniesaha/nexus",
        brief_md="Nexus brief",
        recent_themes_md="Observability",
        key_files=("src/drover/server/observatory.py",),
        next_steps_md="Roll out adoption checks",
        session_count=1,
        last_activity_at=datetime.now(timezone.utc),
        generator_model="test-model",
    )
    if embed:
        store = EmbeddingStore(duckdb_path, model="embed")
        with store.connection() as con:
            store.put(con, "sess-1", [0.1, 0.2, *([0.0] * 766)], model="embed")


def test_pipeline_observatory_includes_artifacts_and_project_readiness(
    tmp_path: Path, pg_control_path: Path, postgres_dsn: str
) -> None:
    _, duckdb_path = _seed(tmp_path)
    vectors = pgvector_available(postgres_dsn)
    _seed_memory(duckdb_path, embed=vectors)

    payload = pipeline_observatory_snapshot(duckdb_path=duckdb_path)

    assert payload["memory"]["available"] is True
    summaries = payload["artifacts"]["session_summaries"]
    assert summaries["total"] == 1 and summaries["bundle_ready"] == 1
    summary = summaries["latest"][0]
    assert summary["session_id"] == "sess-1"
    assert summary["bundle_ready"] is True
    assert summary["repo_owner"] == "arniesaha"
    assert summary["branch"] == "main"
    assert "Built drilldown" in summary["summary_preview"]

    brief = payload["artifacts"]["project_briefs"]["latest"][0]
    assert brief["project_key"] == "arniesaha/nexus"
    assert payload["artifacts"]["project_briefs"]["total"] == 1

    project = payload["projects"][0]
    assert project["project_key"] == "arniesaha/nexus"
    assert project["summary_count"] == 1
    assert project["project_brief_ready"] is True
    # Readiness needs every summary embedded, which needs pgvector.
    assert project["session_embedding_count"] == (1 if vectors else 0)
    assert project["ready"] is vectors


def test_pipeline_observatory_reads_memory_for_a_snapshot_copy(
    tmp_path: Path, pg_control_path: Path
) -> None:
    """A private DuckDB copy has no store registered; the live path supplies it."""
    _, duckdb_path = _seed(tmp_path)
    _seed_memory(duckdb_path, embed=False)
    copy = tmp_path / "copy" / "drover.duckdb"
    copy.parent.mkdir()
    copy.write_bytes(duckdb_path.read_bytes())

    payload = pipeline_observatory_snapshot(
        duckdb_path=copy, memory_store_path=duckdb_path
    )

    assert payload["artifacts"]["session_summaries"]["total"] == 1


def test_cli_observatory_degrades_without_postgres(tmp_path: Path) -> None:
    """A DuckDB control store: tasks still drill down, memory says unavailable."""
    _, duckdb_path = _seed(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"""
        [paths]
        incoming_dir = "{tmp_path / 'incoming'}"
        parquet_dir = "{tmp_path / 'parquet'}"
        duckdb_path = "{duckdb_path}"
        """)

    res = CliRunner().invoke(main, ["--config", str(cfg), "observatory"])

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["memory"]["available"] is False
    assert "PostgreSQL" in payload["memory"]["detail"]
    assert payload["artifacts"]["session_summaries"]["total"] == 0
    assert payload["projects"][0]["project_key"] == "arniesaha/nexus"
    assert payload["projects"][0]["ready"] is False


def test_pipeline_observatory_handles_missing_db(tmp_path: Path) -> None:
    payload = pipeline_observatory_snapshot(duckdb_path=tmp_path / "missing.duckdb")

    assert payload["artifacts"]["session_summaries"]["total"] == 0
    assert payload["projects"] == []
