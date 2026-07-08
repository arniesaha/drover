from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from drover.schema import bootstrap
from drover.server.__main__ import main
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
        con.execute("""INSERT INTO session_summaries
               (session_id, task_id, agent_id, ended_at, summary_md, files_touched,
                tools_used, last_user_prompt, last_assistant, next_steps_md,
                open_questions, status, generator_model, generated_at)
               VALUES ('sess-1', 'task-1', 'openclaw-main', now(), 'Built drilldown',
                       ['src/drover/server/observatory.py'], MAP {'pytest': 1},
                       'add artifacts', 'added the artifact surface',
                       'deploy it', [], 'complete', 'test-model', now())""")
        con.execute("""INSERT INTO session_embeddings
               (session_id, embedding, model, dim, embedded_at)
               VALUES ('sess-1', [0.1, 0.2], 'embed', 2, now())""")
        con.execute("""INSERT INTO project_briefs
               (project_key, repo_owner, repo_name, brief_md, recent_themes_md,
                key_files, open_questions, next_steps_md, session_count,
                last_activity_at, generator_model, generated_at)
               VALUES ('arniesaha/nexus', 'arniesaha', 'nexus', 'Nexus brief',
                       'Observability', ['src/drover/server/observatory.py'], [],
                       'Roll out adoption checks', 1, now(), 'test-model', now())""")
    finally:
        con.close()
    return parquet_dir, duckdb_path


def test_pipeline_observatory_includes_artifacts_and_project_readiness(
    tmp_path: Path,
) -> None:
    _, duckdb_path = _seed(tmp_path)

    payload = pipeline_observatory_snapshot(duckdb_path=duckdb_path)

    summary = payload["artifacts"]["session_summaries"]["latest"][0]
    assert summary["session_id"] == "sess-1"
    assert summary["bundle_ready"] is True
    assert summary["repo_owner"] == "arniesaha"
    assert "Built drilldown" in summary["summary_preview"]

    brief = payload["artifacts"]["project_briefs"]["latest"][0]
    assert brief["project_key"] == "arniesaha/nexus"

    project = payload["projects"][0]
    assert project["project_key"] == "arniesaha/nexus"
    assert project["ready"] is True


def test_cli_observatory_emits_json(tmp_path: Path) -> None:
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
    assert '"session_id": "sess-1"' in res.output
    assert '"project_key": "arniesaha/nexus"' in res.output


def test_pipeline_observatory_handles_missing_db(tmp_path: Path) -> None:
    payload = pipeline_observatory_snapshot(duckdb_path=tmp_path / "missing.duckdb")

    assert payload["artifacts"]["session_summaries"]["total"] == 0
    assert payload["projects"] == []
