"""Tests for session span-tree reconstruction."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from drover.schema import bootstrap
from drover.server.__main__ import main

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
        ("cost_usd", pa.float64()),
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


def _seed_spans(tmp_path: Path) -> Path:
    cfg = _make_config(tmp_path)
    parquet_dir = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=tmp_path / "drover.duckdb")
    base = datetime(2026, 5, 28, 12, tzinfo=timezone.utc)
    common = {
        "trace_id": "trace-1",
        "service_name": "agentweave",
        "session_id": "sess-graph",
        "task_id": "task-1",
        "agent_id": "test-agent",
        "cost_usd": None,
    }
    rows = [
        {
            **common,
            "span_id": "root",
            "parent_span_id": None,
            "name": "session",
            "start_time": base,
            "end_time": base + timedelta(seconds=4),
            "duration_ms": 4000.0,
            "dedup_key": "root",
        },
        {
            **common,
            "span_id": "tool",
            "parent_span_id": "root",
            "name": "tool_call",
            "start_time": base + timedelta(seconds=1),
            "end_time": base + timedelta(seconds=2),
            "duration_ms": 1000.0,
            "dedup_key": "tool",
        },
        {
            **common,
            "span_id": "llm",
            "parent_span_id": "root",
            "name": "llm_call",
            "start_time": base + timedelta(seconds=2),
            "end_time": base + timedelta(seconds=3),
            "duration_ms": 1000.0,
            "dedup_key": "llm",
        },
        {
            **common,
            "span_id": "nested",
            "parent_span_id": "tool",
            "name": "nested",
            "start_time": base + timedelta(seconds=1, milliseconds=250),
            "end_time": base + timedelta(seconds=1, milliseconds=500),
            "duration_ms": 250.0,
            "dedup_key": "nested",
        },
        {
            **common,
            "span_id": "other-session",
            "parent_span_id": None,
            "name": "ignored",
            "session_id": "sess-other",
            "start_time": base,
            "end_time": base,
            "duration_ms": 0.0,
            "dedup_key": "other",
        },
    ]
    _write_spans(parquet_dir, rows)
    return cfg


def test_session_graph_ascii_reconstructs_parent_child_tree(tmp_path: Path) -> None:
    cfg = _seed_spans(tmp_path)
    res = CliRunner().invoke(
        main, ["--config", str(cfg), "session", "graph", "sess-graph"]
    )

    assert res.exit_code == 0, res.output
    assert "sess-graph" in res.output
    assert "└─ session [root]" in res.output
    assert "   ├─ tool_call [tool]" in res.output
    assert "   │  └─ nested [nested]" in res.output
    assert "   └─ llm_call [llm]" in res.output
    assert "ignored" not in res.output


def test_session_graph_json_output_is_nested(tmp_path: Path) -> None:
    cfg = _seed_spans(tmp_path)
    res = CliRunner().invoke(
        main,
        ["--config", str(cfg), "session", "graph", "sess-graph", "--format", "json"],
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["session_id"] == "sess-graph"
    assert payload["span_count"] == 4
    root = payload["roots"][0]
    assert root["span_id"] == "root"
    assert [child["span_id"] for child in root["children"]] == ["tool", "llm"]
    assert root["children"][0]["children"][0]["span_id"] == "nested"


def test_session_graph_dot_output_contains_edges(tmp_path: Path) -> None:
    cfg = _seed_spans(tmp_path)
    res = CliRunner().invoke(
        main,
        ["--config", str(cfg), "session", "graph", "sess-graph", "--format", "dot"],
    )

    assert res.exit_code == 0, res.output
    assert "digraph" in res.output
    assert '"root" -> "tool"' in res.output
    assert '"tool" -> "nested"' in res.output


def test_session_graph_parent_lookup_is_trace_scoped(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    parquet_dir = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=tmp_path / "drover.duckdb")
    base = datetime(2026, 5, 28, 12, tzinfo=timezone.utc)
    common = {
        "service_name": "agentweave",
        "session_id": "sess-collide",
        "task_id": "task-1",
        "agent_id": "test-agent",
        "cost_usd": None,
    }
    _write_spans(
        parquet_dir,
        [
            {
                **common,
                "trace_id": "trace-a",
                "span_id": "root",
                "parent_span_id": None,
                "name": "root-a",
                "start_time": base,
                "end_time": base + timedelta(seconds=3),
                "duration_ms": 3000.0,
                "dedup_key": "root-a",
            },
            {
                **common,
                "trace_id": "trace-a",
                "span_id": "child-a",
                "parent_span_id": "root",
                "name": "child-a",
                "start_time": base + timedelta(seconds=1),
                "end_time": base + timedelta(seconds=2),
                "duration_ms": 1000.0,
                "dedup_key": "child-a",
            },
            {
                **common,
                "trace_id": "trace-b",
                "span_id": "root",
                "parent_span_id": None,
                "name": "root-b",
                "start_time": base + timedelta(seconds=4),
                "end_time": base + timedelta(seconds=5),
                "duration_ms": 1000.0,
                "dedup_key": "root-b",
            },
        ],
    )

    res = CliRunner().invoke(
        main,
        [
            "--config",
            str(cfg),
            "session",
            "graph",
            "sess-collide",
            "--format",
            "json",
        ],
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    roots = {(root["trace_id"], root["name"]): root for root in payload["roots"]}
    assert roots[("trace-a", "root-a")]["children"][0]["span_id"] == "child-a"
    assert roots[("trace-b", "root-b")]["children"] == []


def test_session_graph_exits_nonzero_for_missing_session(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=tmp_path / "drover.duckdb")

    res = CliRunner().invoke(
        main, ["--config", str(cfg), "session", "graph", "missing"]
    )

    assert res.exit_code != 0
    assert "no spans found for session_id=missing" in res.output
