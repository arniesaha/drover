"""Fixture-backed dogfood smoke tests for MCP handoff readiness."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from drover.schema import bootstrap
from drover.server.dogfood_smoke import render_report, run_smoke
from drover.task_id import compute_task_id

_EVENT_SCHEMA = pa.schema(
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
        ("agent_type", pa.string()),
        ("agent_model", pa.string()),
        ("associated_with", pa.string()),
        ("activity_type", pa.string()),
        ("parent_session_id", pa.string()),
        ("project", pa.string()),
        ("task_label", pa.string()),
        ("llm_provider", pa.string()),
        ("llm_model", pa.string()),
        ("stop_reason", pa.string()),
        ("repo_owner", pa.string()),
        ("repo_name", pa.string()),
        ("branch", pa.string()),
        ("principal_id", pa.string()),
        ("prompt_preview", pa.string()),
        ("response_preview", pa.string()),
        ("cost_usd", pa.float64()),
        ("prompt_tokens", pa.int64()),
        ("completion_tokens", pa.int64()),
        ("total_tokens", pa.int64()),
        ("cache_read_tokens", pa.int64()),
        ("cache_write_tokens", pa.int64()),
        ("attributes_json", pa.string()),
        ("raw_object_uri", pa.string()),
        ("dedup_key", pa.string()),
    ]
)


def _seed_fixture(tmp_path: Path, *, include_active: bool = True) -> dict[str, object]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    now = datetime.now(timezone.utc)
    repo_owner = "arniesaha"
    repo_name = "nexus"
    branch = "main"
    task_id = compute_task_id(None, repo_owner, repo_name, branch)

    second_event_ts = now - timedelta(minutes=10 if include_active else 40)
    _write_events(
        parquet_dir,
        [
            {
                "id": "event-a1",
                "session_id": "sess-A",
                "agent_id": "nas-claude",
                "task_id": task_id,
                "timestamp": now - timedelta(hours=3),
                "event_type": "user_message",
                "role": "user",
                "content": "Implement MCP handoff fixture coverage.",
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "branch": branch,
                "principal_id": "arnab",
                "dedup_key": "event-a1",
                "raw_data": "{}",
            },
            {
                "id": "event-b1",
                "session_id": "sess-B",
                "agent_id": "work-macbook-claude",
                "task_id": task_id,
                "timestamp": second_event_ts,
                "event_type": "assistant_message",
                "role": "assistant",
                "content": "Continuing the handoff smoke from another agent.",
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "branch": branch,
                "principal_id": "arnab",
                "dedup_key": "event-b1",
                "raw_data": "{}",
            },
        ],
    )
    span_start = second_event_ts + timedelta(minutes=1)
    _write_span(
        parquet_dir,
        trace_id="trace-1",
        span_id="span-1",
        parent_span_id=None,
        name="agent.turn",
        service_name="agentweave-proxy",
        start_time=span_start,
        end_time=span_start + timedelta(minutes=1),
        duration_ms=60_000.0,
        session_id="sess-B",
        task_id="agentweave-task",
        agent_id="work-macbook-claude",
        agent_type="claude-code",
        agent_model="claude-sonnet",
        associated_with=None,
        activity_type="coding",
        parent_session_id=None,
        project="nexus",
        task_label="handoff-smoke",
        llm_provider="anthropic",
        llm_model="claude-sonnet",
        stop_reason="end_turn",
        repo_owner=repo_owner,
        repo_name=repo_name,
        branch=branch,
        principal_id="arnab",
        prompt_preview="Continue the MCP handoff smoke.",
        response_preview="Added fixture coverage.",
        cost_usd=0.12,
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cache_read_tokens=0,
        cache_write_tokens=0,
        attributes_json=json.dumps({"workspaceDir": "/Users/arnabmac/jenny/nexus"}),
        raw_object_uri="fixture://span-1",
        dedup_key="span-1",
    )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            """INSERT INTO tasks
               (task_id, repo_owner, repo_name, branch, principal_id, status,
                created_at, last_activity_at, session_count, total_cost_usd)
               VALUES (?, ?, ?, ?, 'arnab', 'open', ?, ?, 2, 0.12)""",
            [task_id, repo_owner, repo_name, branch, now - timedelta(hours=3), now],
        )
        con.execute(
            """INSERT INTO session_summaries
               (session_id, task_id, agent_id, ended_at, summary_md, files_touched,
                tools_used, last_user_prompt, last_assistant, next_steps_md,
                open_questions, status, generator_model, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, MAP{'Edit': 2}, ?, ?, ?, ?, ?, ?, ?)""",
            [
                "sess-A",
                task_id,
                "nas-claude",
                now - timedelta(hours=2),
                "Built fixture-backed MCP handoff checks for another agent.",
                ["src/nexus/server/dogfood_smoke.py"],
                "Implement MCP handoff fixture coverage.",
                "Continuing from the NAS agent summary.",
                "Run the smoke against live ~/.nexus/nexus.duckdb before OSS readiness.",
                ["Should data quality be required before #82 merges?"],
                "completed",
                "test-model",
                now - timedelta(hours=2),
            ],
        )
    finally:
        con.close()

    return {
        "duckdb_path": duckdb_path,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch": branch,
        "project_key": f"{repo_owner}/{repo_name}",
    }


def _write_events(parquet_dir: Path, rows: list[dict]) -> None:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        date = row["timestamp"].date().isoformat()
        grouped.setdefault((date, row["agent_id"]), []).append(row)

    for (date, agent_id), part_rows in grouped.items():
        out = parquet_dir / "agent_events" / f"date={date}" / f"agent_id={agent_id}"
        out.mkdir(parents=True, exist_ok=True)
        cols = {
            field.name: pa.array(
                [row.get(field.name) for row in part_rows],
                type=field.type,
            )
            for field in _EVENT_SCHEMA
        }
        pq.write_table(pa.table(cols, schema=_EVENT_SCHEMA), out / "part-test.parquet")


def _write_span(parquet_dir: Path, **row) -> None:
    date = row["start_time"].date().isoformat()
    out = parquet_dir / "spans" / f"date={date}"
    out.mkdir(parents=True, exist_ok=True)
    cols = {
        field.name: pa.array([row.get(field.name)], type=field.type)
        for field in _SPAN_SCHEMA
    }
    pq.write_table(
        pa.table(cols, schema=_SPAN_SCHEMA), out / f"{row['span_id']}.parquet"
    )


def _status_by_name(report: dict) -> dict[str, dict]:
    return {check["name"]: check for check in report["checks"]}


def test_smoke_passes_against_fixture_and_checks_data_quality(
    tmp_path: Path,
) -> None:
    fixture = _seed_fixture(tmp_path)

    report = run_smoke(
        duckdb_path=fixture["duckdb_path"],
        repo_owner=fixture["repo_owner"],
        repo_name=fixture["repo_name"],
        branch=fixture["branch"],
        project_key=fixture["project_key"],
        replay_session_id="sess-A",
        since="2026-05-01T00:00:00+00:00",
    )

    checks = _status_by_name(report)
    assert report["status"] == "pass"
    assert checks["handoff"]["status"] == "pass"
    assert checks["session_replay"]["status"] == "pass"
    assert checks["project_activity"]["status"] == "pass"
    assert checks["data_quality"]["status"] == "pass"

    rendered = render_report(report)
    assert "PASS dogfood MCP smoke" in rendered
    assert "PASS data_quality" in rendered
    assert str(fixture["duckdb_path"]) in rendered


def test_smoke_allows_historical_handoff_without_active_session(
    tmp_path: Path,
) -> None:
    fixture = _seed_fixture(tmp_path, include_active=False)

    def passing_quality_check(*, duckdb_path: Path, project_key: str) -> dict:
        assert duckdb_path == fixture["duckdb_path"]
        assert project_key == fixture["project_key"]
        return {"status": "pass"}

    report = run_smoke(
        duckdb_path=fixture["duckdb_path"],
        repo_owner=fixture["repo_owner"],
        repo_name=fixture["repo_name"],
        branch=fixture["branch"],
        project_key=fixture["project_key"],
        replay_session_id="sess-A",
        since="2026-05-01T00:00:00+00:00",
        quality_check=passing_quality_check,
    )

    checks = _status_by_name(report)
    assert report["status"] == "pass"
    assert checks["handoff"]["status"] == "pass"
    assert "0 active sessions" in checks["handoff"]["message"]


def test_smoke_reports_specific_failed_dimensions(tmp_path: Path) -> None:
    fixture = _seed_fixture(tmp_path)

    report = run_smoke(
        duckdb_path=fixture["duckdb_path"],
        repo_owner=fixture["repo_owner"],
        repo_name=fixture["repo_name"],
        branch=fixture["branch"],
        project_key=fixture["project_key"],
        replay_session_id="missing-session",
        since="2026-05-01T00:00:00+00:00",
    )

    checks = _status_by_name(report)
    assert report["status"] == "fail"
    assert checks["session_replay"]["status"] == "fail"
    assert checks["session_replay"]["dimensions"] == ["replay_availability"]
    assert "missing-session" in checks["session_replay"]["message"]

    rendered = render_report(report)
    assert "FAIL dogfood MCP smoke" in rendered
    assert "FAIL session_replay [replay_availability]" in rendered


def test_smoke_surfaces_failing_quality_check(tmp_path: Path) -> None:
    fixture = _seed_fixture(tmp_path)

    def fake_quality_check(*, duckdb_path: Path, project_key: str) -> dict:
        assert duckdb_path == fixture["duckdb_path"]
        assert project_key == fixture["project_key"]
        return {
            "status": "fail",
            "dimensions": ["summary_freshness", "replay_coverage"],
            "message": "project summaries are stale",
        }

    report = run_smoke(
        duckdb_path=fixture["duckdb_path"],
        repo_owner=fixture["repo_owner"],
        repo_name=fixture["repo_name"],
        branch=fixture["branch"],
        project_key=fixture["project_key"],
        replay_session_id="sess-A",
        since="2026-05-01T00:00:00+00:00",
        quality_check=fake_quality_check,
    )

    checks = _status_by_name(report)
    assert report["status"] == "fail"
    assert checks["data_quality"]["status"] == "fail"
    assert checks["data_quality"]["dimensions"] == [
        "summary_freshness",
        "replay_coverage",
    ]
    assert checks["data_quality"]["message"] == "project summaries are stale"

    rendered = render_report(report)
    assert "FAIL data_quality [summary_freshness, replay_coverage]" in rendered
    assert "project summaries are stale" in rendered
