"""Tests for deriving lakehouse decisions from explicitly marked spans."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from drover.schema import bootstrap
from drover.server.__main__ import main
from drover.server.decisions import derive_decisions

_SPAN_SCHEMA = pa.schema(
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
        ("repo_owner", pa.string()),
        ("repo_name", pa.string()),
        ("branch", pa.string()),
        ("prompt_preview", pa.string()),
        ("response_preview", pa.string()),
        ("attributes_json", pa.string()),
        ("dedup_key", pa.string()),
    ]
)


def _make_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"""
[paths]
incoming_dir = "{tmp_path / 'incoming'}"
parquet_dir  = "{tmp_path / 'parquet'}"
duckdb_path  = "{tmp_path / 'drover.duckdb'}"
processed_retention_days = 7

[server]
otlp_grpc_port = 14317
mcp_http_port  = 17077

[agent]
agent_id     = "test"
principal_id = "test"
""")
    return cfg


def _write_spans(parquet_dir: Path, rows: list[dict]) -> None:
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        by_date.setdefault(row["start_time"].date().isoformat(), []).append(row)
    for date, date_rows in by_date.items():
        out = parquet_dir / "spans" / f"date={date}"
        out.mkdir(parents=True, exist_ok=True)
        cols = {
            field.name: pa.array(
                [row.get(field.name) for row in date_rows], type=field.type
            )
            for field in _SPAN_SCHEMA
        }
        pq.write_table(pa.table(cols, schema=_SPAN_SCHEMA), out / "part.parquet")


def _seed_decision_trace(tmp_path: Path) -> Path:
    cfg = _make_config(tmp_path)
    parquet_dir = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=tmp_path / "drover.duckdb")
    base = datetime(2026, 5, 28, 12, tzinfo=timezone.utc)
    common = {
        "trace_id": "trace-decision",
        "service_name": "agentweave",
        "session_id": "sess-decision",
        "task_id": "task-17",
        "agent_id": "agent-a",
        "repo_owner": "nous",
        "repo_name": "nexus",
        "branch": "feat-17",
    }
    _write_spans(
        parquet_dir,
        [
            {
                **common,
                "span_id": "turn",
                "parent_span_id": None,
                "name": "agent_turn",
                "start_time": base,
                "end_time": base + timedelta(seconds=10),
                "duration_ms": 10000.0,
                "prompt_preview": "implement issue 17",
                "response_preview": None,
                "attributes_json": json.dumps({"turn": 1}),
                "dedup_key": "turn",
            },
            {
                **common,
                "span_id": "llm",
                "parent_span_id": "turn",
                "name": "llm_call",
                "start_time": base + timedelta(seconds=1),
                "end_time": base + timedelta(seconds=4),
                "duration_ms": 3000.0,
                "prompt_preview": "Should decisions be hallucinated from arbitrary text?",
                "response_preview": "No; use explicit decision attributes only.",
                "attributes_json": json.dumps(
                    {
                        "nexus.decision.id": "dec-17-a",
                        "nexus.decision.statement": "Use explicit span attributes for the first decision extraction slice.",
                        "nexus.decision.rationale": "This keeps extraction deterministic and avoids hallucinating decisions from arbitrary text.",
                        "nexus.decision.alternatives": [
                            "LLM summarize every agent turn",
                            "Regex arbitrary response text",
                        ],
                    }
                ),
                "dedup_key": "llm",
            },
            {
                **common,
                "span_id": "tool",
                "parent_span_id": "turn",
                "name": "tool_call",
                "start_time": base + timedelta(seconds=5),
                "end_time": base + timedelta(seconds=6),
                "duration_ms": 1000.0,
                "prompt_preview": None,
                "response_preview": None,
                "attributes_json": json.dumps(
                    {
                        "tool.name": "patch",
                        "tool.action": "create decisions table and deterministic extractor",
                    }
                ),
                "dedup_key": "tool",
            },
            {
                **common,
                "trace_id": "trace-no-marker",
                "span_id": "unmarked-turn",
                "parent_span_id": None,
                "name": "agent_turn",
                "start_time": base + timedelta(minutes=1),
                "end_time": base + timedelta(minutes=1, seconds=1),
                "duration_ms": 1000.0,
                "prompt_preview": None,
                "response_preview": "I decided to say this unmarked text should not produce a row.",
                "attributes_json": json.dumps({}),
                "dedup_key": "unmarked-turn",
            },
        ],
    )
    return cfg


def test_bootstrap_creates_decisions_table(tmp_path: Path) -> None:
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=tmp_path / "drover.duckdb")
    con = duckdb.connect(str(tmp_path / "drover.duckdb"), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    finally:
        con.close()


def test_derive_decisions_from_marked_llm_child_and_selected_tool(
    tmp_path: Path,
) -> None:
    _seed_decision_trace(tmp_path)

    inserted = derive_decisions(
        duckdb_path=tmp_path / "drover.duckdb", parquet_dir=tmp_path / "parquet"
    )

    assert inserted == 1
    con = duckdb.connect(str(tmp_path / "drover.duckdb"), read_only=True)
    try:
        row = con.execute("""
            SELECT decision_id, trace_id, root_span_id, source_span_id,
                   decision_statement, rationale, alternatives, selected_action,
                   session_id, task_id, agent_id, repo_owner, repo_name, branch
            FROM decisions
            """).fetchone()
    finally:
        con.close()

    assert row == (
        "dec-17-a",
        "trace-decision",
        "turn",
        "llm",
        "Use explicit span attributes for the first decision extraction slice.",
        "This keeps extraction deterministic and avoids hallucinating decisions from arbitrary text.",
        ["LLM summarize every agent turn", "Regex arbitrary response text"],
        "patch: create decisions table and deterministic extractor",
        "sess-decision",
        "task-17",
        "agent-a",
        "nous",
        "nexus",
        "feat-17",
    )

    # Idempotent reruns should not duplicate the derived decision.
    assert (
        derive_decisions(
            duckdb_path=tmp_path / "drover.duckdb", parquet_dir=tmp_path / "parquet"
        )
        == 0
    )


def test_decisions_derive_cli_reports_inserted_count(tmp_path: Path) -> None:
    cfg = _seed_decision_trace(tmp_path)

    res = CliRunner().invoke(main, ["--config", str(cfg), "decisions", "derive"])

    assert res.exit_code == 0, res.output
    assert "inserted 1 decision" in res.output
