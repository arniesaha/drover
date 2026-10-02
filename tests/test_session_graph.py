"""Tests for the Session Graph: delegation tree, and the legacy span tree."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from drover.schema import bootstrap
from drover.server.__main__ import main
from drover.server.harness.registry import HarnessRegistry
from drover.server.session_graph import delegation_graph_payload, session_state

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


def _make_config(tmp_path: Path, *, spans: bool = True) -> Path:
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

[telemetry]
spans_enabled = {"true" if spans else "false"}
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
        main, ["--config", str(cfg), "session", "graph", "--spans", "sess-graph"]
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
        [
            "--config",
            str(cfg),
            "session",
            "graph",
            "--spans",
            "sess-graph",
            "--format",
            "json",
        ],
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
        [
            "--config",
            str(cfg),
            "session",
            "graph",
            "--spans",
            "sess-graph",
            "--format",
            "dot",
        ],
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
            "--spans",
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
        main, ["--config", str(cfg), "session", "graph", "--spans", "missing"]
    )

    assert res.exit_code != 0
    assert "no spans found for session_id=missing" in res.output


def test_span_tree_requires_the_span_integration(tmp_path: Path) -> None:
    cfg = _seed_spans(tmp_path)
    _make_config(tmp_path, spans=False)

    res = CliRunner().invoke(
        main, ["--config", str(cfg), "session", "graph", "--spans", "sess-graph"]
    )

    assert res.exit_code != 0
    assert "spans_enabled = true" in res.output


# --- Delegation graph (launch metadata only) -----------------------------------

_NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def _registry(tmp_path: Path) -> HarnessRegistry:
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    return HarnessRegistry(duckdb_path)


def _launch(registry: HarnessRegistry, session_id: str, **fields) -> None:
    registry.create_session(
        session_id=session_id,
        host_id=fields.pop("host_id", "mac-mini"),
        harness=fields.pop("harness", "claude-code"),
        command="claude",
        status=fields.pop("status", "running"),
        started_at=fields.pop("started_at", _NOW - timedelta(minutes=10)),
        repo_owner="acme",
        repo_name="widget",
        **fields,
    )


def test_session_state_maps_recorded_fields_to_actionable_states() -> None:
    recent = _NOW - timedelta(minutes=5)
    stale = _NOW - timedelta(hours=2)
    assert (
        session_state(status="completed", awaiting=None, last_activity=stale, now=_NOW)
        == "done"
    )
    assert (
        session_state(status="errored", awaiting=None, last_activity=recent, now=_NOW)
        == "failed"
    )
    assert (
        session_state(status="running", awaiting="input", last_activity=stale, now=_NOW)
        == "awaiting_input"
    )
    assert (
        session_state(
            status="running", awaiting="approval", last_activity=recent, now=_NOW
        )
        == "awaiting_approval"
    )
    assert (
        session_state(status="running", awaiting=None, last_activity=stale, now=_NOW)
        == "idle"
    )
    assert (
        session_state(status="running", awaiting=None, last_activity=recent, now=_NOW)
        == "running"
    )


def test_delegation_graph_builds_tree_from_parent_and_handoff_links(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    _launch(registry, "orchestrator")
    _launch(registry, "child-a", parent_session_id="orchestrator")
    _launch(registry, "child-b", parent_session_id="orchestrator", status="errored")
    _launch(
        registry,
        "grandchild",
        source_session_id="child-a",
        handoff_mode="nexus_handoff",
    )
    registry.update_session_activity(
        "child-a", awaiting="input", last_activity=_NOW - timedelta(minutes=1)
    )

    # Asking from a leaf returns the whole tree, rooted at the orchestrator.
    payload = delegation_graph_payload(registry, session_id="grandchild", now=_NOW)

    assert payload is not None
    assert payload["source"] == "drover_launch_metadata"
    assert payload["focus_session_id"] == "grandchild"
    root = payload["root"]
    assert root["kind"] == "session"
    assert root["started_by"] == "direct launch"
    top = root["children"][0]
    assert top["session_id"] == "orchestrator"
    children = {node["session_id"]: node for node in top["children"]}
    assert set(children) == {"child-a", "child-b"}
    assert children["child-a"]["link"] == "delegated"
    assert children["child-a"]["state"] == "awaiting_input"
    assert children["child-b"]["state"] == "failed"
    assert children["child-a"]["children"][0]["session_id"] == "grandchild"
    assert children["child-a"]["children"][0]["link"] == "handoff"
    assert payload["node_count"] == 4
    stuck = {item["session_id"]: item["reason"] for item in payload["stuck"]}
    assert stuck == {"child-a": "waiting for input", "child-b": "failed"}


def test_delegation_graph_names_an_unknown_handoff_source_without_inventing_it(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    _launch(
        registry,
        "resumed",
        source_session_id="native-123",
        handoff_mode="native_resume",
    )

    payload = delegation_graph_payload(registry, session_id="resumed", now=_NOW)

    assert payload["root"]["started_by"] == "handoff from native-123"
    assert payload["root"]["unknown_parent_session_id"] == "native-123"
    assert payload["root"]["children"][0]["session_id"] == "resumed"


def test_delegation_graph_groups_a_factory_run(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    for session_id, revision in (("run-a", 1), ("run-b", 2)):
        _launch(
            registry,
            session_id,
            source_session_id=f"factory/run_42@{revision}",
            handoff_mode="factory_observer",
        )
    _launch(
        registry,
        "other-run",
        source_session_id="factory/run_43@1",
        handoff_mode="factory_observer",
    )

    by_session = delegation_graph_payload(registry, session_id="run-b", now=_NOW)
    by_run = delegation_graph_payload(registry, run_id="run_42", now=_NOW)

    for payload in (by_session, by_run):
        assert payload["root"] == {
            **payload["root"],
            "kind": "factory_run",
            "run_id": "run_42",
        }
        assert [n["session_id"] for n in payload["root"]["children"]] == [
            "run-a",
            "run-b",
        ]
    assert by_session["focus_session_id"] == "run-b"


def test_delegation_graph_is_capped(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _launch(registry, "root")
    for index in range(5):
        _launch(registry, f"child-{index}", parent_session_id="root")

    payload = delegation_graph_payload(
        registry, session_id="root", now=_NOW, max_nodes=3
    )

    assert payload["node_count"] == 3
    assert payload["truncated"] is True


def test_delegation_graph_returns_none_for_unknown_session(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    assert delegation_graph_payload(registry, session_id="missing") is None


def test_session_graph_cli_defaults_to_delegation_tree_with_spans_off(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path, spans=False)
    registry = _registry(tmp_path)
    _launch(registry, "orchestrator")
    _launch(registry, "worker", parent_session_id="orchestrator")

    text = CliRunner().invoke(
        main, ["--config", str(cfg), "session", "graph", "worker"]
    )
    as_json = CliRunner().invoke(
        main,
        ["--config", str(cfg), "session", "graph", "worker", "--format", "json"],
    )
    missing = CliRunner().invoke(
        main, ["--config", str(cfg), "session", "graph", "nope"]
    )

    assert text.exit_code == 0, text.output
    assert text.output.startswith("orchestrator [")
    assert "└─ worker [" in text.output
    assert "(delegated)" in text.output
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.output)["node_count"] == 2
    assert missing.exit_code != 0
    assert "no harness session found for session_id=nope" in missing.output
