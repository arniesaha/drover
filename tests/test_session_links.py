"""Tests for deriving Claude JSONL <-> AgentWeave session links."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from drover.schema import bootstrap


@pytest.fixture
def tmp_lakehouse(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _write_agent_events(parquet_dir: Path, rows: list[dict]) -> None:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        ts = row["timestamp"]
        grouped.setdefault((ts.strftime("%Y-%m-%d"), row["agent_id"]), []).append(row)

    for (date, agent_id), part_rows in grouped.items():
        out_dir = parquet_dir / "agent_events" / f"date={date}" / f"agent_id={agent_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = [
            {k: v for k, v in row.items() if k not in {"date", "agent_id"}}
            for row in part_rows
        ]
        pq.write_table(pa.Table.from_pylist(payload), out_dir / "part-test.parquet")


def _write_spans(parquet_dir: Path, rows: list[dict]) -> None:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["start_time"].strftime("%Y-%m-%d"), []).append(row)

    for date, part_rows in grouped.items():
        out_dir = parquet_dir / "spans" / f"date={date}"
        out_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(part_rows), out_dir / "part-test.parquet")


def _agent_event(
    session_id: str,
    timestamp: str,
    *,
    agent_id: str = "nas-claude",
    repo_owner: str = "arniesaha",
    repo_name: str = "nexus",
    branch: str = "main",
) -> dict:
    return {
        "id": f"evt-{session_id}-{timestamp}",
        "session_id": session_id,
        "agent_id": agent_id,
        "timestamp": _ts(timestamp),
        "event_type": "message",
        "role": "user",
        "content": "work on session links",
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch": branch,
        "task_id": "task-1",
        "principal_id": None,
        "dedup_key": f"dedup-{session_id}-{timestamp}",
        "raw_data": "{}",
    }


def _span(
    span_id: str,
    logical_session_id: str,
    start_time: str,
    *,
    parent_session_id: str | None = None,
    agent_id: str = "nas-claude",
    repo_owner: str = "arniesaha",
    repo_name: str = "nexus",
    branch: str = "main",
) -> dict:
    start = _ts(start_time)
    return {
        "trace_id": f"trace-{span_id}",
        "span_id": span_id,
        "parent_span_id": None,
        "name": "llm.call",
        "service_name": "agentweave-proxy",
        "start_time": start,
        "end_time": start,
        "duration_ms": 1.0,
        "session_id": logical_session_id,
        "task_id": None,
        "agent_id": agent_id,
        "agent_type": "claude",
        "agent_model": "claude",
        "associated_with": None,
        "activity_type": "llm_call",
        "parent_session_id": parent_session_id,
        "project": None,
        "task_label": None,
        "llm_provider": "anthropic",
        "llm_model": "claude",
        "stop_reason": None,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch": branch,
        "principal_id": None,
        "prompt_preview": None,
        "response_preview": None,
        "cost_usd": 0.01,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "attributes_json": "{}",
        "raw_object_uri": "tempo://trace",
        "dedup_key": f"span-{span_id}",
    }


def test_session_links_direct_parent_session_id_match(tmp_lakehouse):
    parquet_dir, duckdb_path = tmp_lakehouse
    _write_agent_events(
        parquet_dir,
        [
            _agent_event("claude-jsonl-uuid-1", "2026-05-26T12:00:00Z"),
            _agent_event("claude-jsonl-uuid-1", "2026-05-26T12:10:00Z"),
        ],
    )
    _write_spans(
        parquet_dir,
        [
            _span(
                "span-1",
                "claude-code-nas-main",
                "2026-05-26T12:05:00Z",
                parent_session_id="claude-jsonl-uuid-1",
            )
        ],
    )

    con = duckdb.connect(str(duckdb_path))
    rows = con.execute("""
        SELECT source_session_id, target_session_id, confidence, reason,
               first_seen_at, last_seen_at
        FROM session_links
        """).fetchall()

    assert rows == [
        (
            "claude-jsonl-uuid-1",
            "claude-code-nas-main",
            pytest.approx(1.0),
            "parent_session_id",
            _ts("2026-05-26T12:05:00Z"),
            _ts("2026-05-26T12:05:00Z"),
        )
    ]


def test_session_links_time_window_match_requires_same_repo_and_agent(tmp_lakehouse):
    parquet_dir, duckdb_path = tmp_lakehouse
    _write_agent_events(
        parquet_dir,
        [
            _agent_event("claude-jsonl-uuid-2", "2026-05-26T13:00:00Z"),
            _agent_event("claude-jsonl-uuid-2", "2026-05-26T13:12:00Z"),
        ],
    )
    _write_spans(
        parquet_dir,
        [
            _span("span-2", "claude-code-nas-main", "2026-05-26T13:03:00Z"),
            _span(
                "span-other-repo",
                "claude-code-nas-other",
                "2026-05-26T13:04:00Z",
                repo_name="other",
            ),
        ],
    )

    con = duckdb.connect(str(duckdb_path))
    rows = con.execute("""
        SELECT source_session_id, target_session_id, confidence, reason
        FROM session_links
        """).fetchall()

    assert rows == [
        (
            "claude-jsonl-uuid-2",
            "claude-code-nas-main",
            pytest.approx(0.75),
            "agent_repo_time_window",
        )
    ]


def test_session_links_do_not_guess_when_time_window_is_ambiguous(tmp_lakehouse):
    parquet_dir, duckdb_path = tmp_lakehouse
    _write_agent_events(
        parquet_dir,
        [
            _agent_event("claude-jsonl-uuid-3", "2026-05-26T14:00:00Z"),
            _agent_event("claude-jsonl-uuid-3", "2026-05-26T14:10:00Z"),
        ],
    )
    _write_spans(
        parquet_dir,
        [
            _span("span-3a", "claude-code-nas-main", "2026-05-26T14:02:00Z"),
            _span("span-3b", "claude-code-nas-alt", "2026-05-26T14:03:00Z"),
        ],
    )

    con = duckdb.connect(str(duckdb_path))
    rows = con.execute(
        "SELECT source_session_id, target_session_id FROM session_links"
    ).fetchall()

    assert rows == []
