"""Tests for MCP tool query functions in drover.server.mcp.tools."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from drover.schema import bootstrap
from drover.server.mcp import tools as mcp_tools
from drover.server.mcp.tools import (
    drover_active_sessions,
    drover_files_touched,
    drover_handoff,
    drover_search,
    drover_session_replay,
    drover_session_summary,
    drover_task_status,
)
from drover.task_id import compute_task_id


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _write_agent_events(parquet_dir: Path, rows: list[dict]) -> None:
    """Write rows directly to the agent_events Parquet partition tree."""
    grouped: dict[tuple, list[dict]] = {}
    for r in rows:
        date = r["timestamp"].strftime("%Y-%m-%d")
        grouped.setdefault((date, r["agent_id"]), []).append(r)

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
    for (date, agent_id), part_rows in grouped.items():
        d = parquet_dir / "agent_events" / f"date={date}" / f"agent_id={agent_id}"
        d.mkdir(parents=True, exist_ok=True)
        cols = {f.name: [r.get(f.name) for r in part_rows] for f in schema}
        # drop partition columns
        for col in ("date",):
            cols.pop(col, None)
        table = pa.table(
            {k: pa.array(v, type=schema.field(k).type) for k, v in cols.items()},
            schema=schema,
        )
        pq.write_table(table, d / "part-test.parquet", compression="zstd")


def _populate(parquet_dir: Path, duckdb_path: Path) -> dict:
    """Seed two sessions on the same task, with a summary on the older one."""
    now = datetime.now(timezone.utc)
    five_min_ago = now - timedelta(minutes=5)
    two_hours_ago = now - timedelta(hours=2)

    repo_owner, repo_name, branch = "arniesaha", "nexus", "main"
    tid = compute_task_id(None, repo_owner, repo_name, branch)

    rows = [
        # Old session A (closed)
        dict(
            id="ea-1",
            session_id="sess-A",
            agent_id="nas-claude",
            task_id=tid,
            timestamp=two_hours_ago,
            event_type="user_message",
            role="user",
            content="kicked off the lakehouse rewrite",
            repo_owner=repo_owner,
            repo_name=repo_name,
            branch=branch,
            principal_id="arnab",
            dedup_key="ea1",
            raw_data="{}",
        ),
        dict(
            id="ea-2",
            session_id="sess-A",
            agent_id="nas-claude",
            task_id=tid,
            timestamp=two_hours_ago + timedelta(minutes=1),
            event_type="tool_call",
            role="assistant",
            content='{"tool":"Edit","path":"src/foo.py"}',
            repo_owner=repo_owner,
            repo_name=repo_name,
            branch=branch,
            principal_id="arnab",
            dedup_key="ea2",
            raw_data=json.dumps(
                {
                    "tool_use_blocks": [
                        {"name": "Edit", "input": {"file_path": "src/foo.py"}}
                    ]
                }
            ),
        ),
        # Active session B (currently running)
        dict(
            id="eb-1",
            session_id="sess-B",
            agent_id="macmini-claude",
            task_id=tid,
            timestamp=five_min_ago,
            event_type="user_message",
            role="user",
            content="adding the duckdb migration script",
            repo_owner=repo_owner,
            repo_name=repo_name,
            branch=branch,
            principal_id="arnab",
            dedup_key="eb1",
            raw_data="{}",
        ),
    ]
    _write_agent_events(parquet_dir, rows)

    # Bootstrap again so the views see the new partitions
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    con = duckdb.connect(str(duckdb_path))
    try:
        # Insert a task row + a summary on session A
        con.execute(
            """INSERT INTO tasks (task_id, repo_owner, repo_name, branch, principal_id,
                                  status, created_at, last_activity_at, session_count, total_cost_usd)
               VALUES (?, ?, ?, ?, 'arnab', 'open', now(), now(), 2, 0.0)""",
            [tid, repo_owner, repo_name, branch],
        )
        con.execute(
            """INSERT INTO session_summaries (session_id, task_id, agent_id, ended_at, summary_md,
                                              files_touched, tools_used, last_user_prompt,
                                              last_assistant, next_steps_md, open_questions,
                                              status, generator_model, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())""",
            [
                "sess-A",
                tid,
                "nas-claude",
                two_hours_ago,
                "Set up the foundation; ported parsers; landed Plan 1.",
                ["src/foo.py"],
                {"Edit": 1},
                "kicked off the lakehouse rewrite",
                "ok done",
                "Wire OTLP receiver into nexus-server run.",
                ["should we keep BigQuery for 30 days?"],
                "completed",
                "claude-haiku-4-5-20251001",
            ],
        )
    finally:
        con.close()

    return {
        "task_id": tid,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch": branch,
    }


def test_handoff_returns_summary_and_active_sessions(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    ctx = _populate(parquet_dir, duckdb_path)

    out = drover_handoff(
        duckdb_path=duckdb_path,
        repo_owner=ctx["repo_owner"],
        repo_name=ctx["repo_name"],
        branch=ctx["branch"],
    )
    assert out["task_id"] == ctx["task_id"]
    assert any("Plan 1" in s["summary_md"] for s in out["summaries"])
    # session B was emitted within the last 5 minutes — should appear active
    active_ids = {s["session_id"] for s in out["active_sessions"]}
    assert "sess-B" in active_ids
    assert "sess-A" not in active_ids  # has summary → no longer active


def test_handoff_quarantines_unknown_openclaw_active_session(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    now = datetime.now(timezone.utc)
    repo_owner, repo_name, branch = "arniesaha", "openclaw", "main"
    tid = compute_task_id(None, repo_owner, repo_name, branch)
    _write_agent_events(
        parquet_dir,
        [
            dict(
                id="native-openclaw-1",
                session_id="b58fbd05-native-openclaw",
                agent_id="nas-openclaw",
                task_id=tid,
                timestamp=now - timedelta(minutes=2),
                event_type="tool_call",
                role="assistant",
                content="current fixed OpenClaw event",
                repo_owner=repo_owner,
                repo_name=repo_name,
                branch=branch,
                principal_id="arnab",
                dedup_key="native-openclaw-1",
                raw_data=json.dumps(
                    {
                        "harness": "openclaw",
                        "session_uuid": "b58fbd05-native-openclaw",
                        "session_key": "agent:main:main",
                    }
                ),
            ),
            dict(
                id="historical-unknown-openclaw-1",
                session_id="unknown_openclaw",
                agent_id="nas-openclaw",
                task_id=tid,
                timestamp=now - timedelta(minutes=1),
                event_type="tool_call",
                role="assistant",
                content="historical placeholder should not dominate handoff",
                repo_owner=repo_owner,
                repo_name=repo_name,
                branch=branch,
                principal_id="arnab",
                dedup_key="historical-unknown-openclaw-1",
                raw_data='{"type":"message"}',
            ),
        ],
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    out = drover_handoff(
        duckdb_path=duckdb_path,
        repo_owner=repo_owner,
        repo_name=repo_name,
        branch=branch,
    )

    active_ids = {s["session_id"] for s in out["active_sessions"]}
    assert "b58fbd05-native-openclaw" in active_ids
    assert "unknown_openclaw" not in active_ids


def test_handoff_spans_branches_when_branch_omitted(tmp_path: Path) -> None:
    """#53: calling drover_handoff with (repo_owner, repo_name) and no branch
    must return summaries from every branch of that repo, not just the one
    whose task_id happens to match compute_task_id(None, owner, name, None)."""
    parquet_dir, duckdb_path = _seed(tmp_path)
    ctx = _populate(parquet_dir, duckdb_path)

    # Seed a second task on a different branch with its own summary.
    other_tid = compute_task_id(None, ctx["repo_owner"], ctx["repo_name"], "feat/other")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            """INSERT INTO tasks (task_id, repo_owner, repo_name, branch, principal_id,
                                  status, created_at, last_activity_at, session_count, total_cost_usd)
               VALUES (?, ?, ?, 'feat/other', 'arnab', 'open', now(), now(), 1, 0.0)""",
            [other_tid, ctx["repo_owner"], ctx["repo_name"]],
        )
        con.execute(
            """INSERT INTO session_summaries
               (session_id, task_id, agent_id, ended_at, summary_md,
                files_touched, tools_used, last_user_prompt, last_assistant,
                next_steps_md, open_questions, status, generator_model, generated_at)
               VALUES (?, ?, 'macmini-claude', now(), ?, ?, MAP{}, '', '',
                       'continue feature', [], 'completed', 'test', now())""",
            [
                "sess-C",
                other_tid,
                "Worked on feat/other; refactored handler.",
                ["src/other.py"],
            ],
        )
    finally:
        con.close()

    # No-branch query → should see summaries from BOTH branches.
    out = drover_handoff(
        duckdb_path=duckdb_path,
        repo_owner=ctx["repo_owner"],
        repo_name=ctx["repo_name"],
        max_summaries=10,
    )
    session_ids = {s["session_id"] for s in out["summaries"]}
    assert {"sess-A", "sess-C"}.issubset(session_ids)
    assert out["task_id"] is None  # no specific branch → no canonical task hash

    # Branch-scoped query → only that branch's summaries.
    out_main = drover_handoff(
        duckdb_path=duckdb_path,
        repo_owner=ctx["repo_owner"],
        repo_name=ctx["repo_name"],
        branch="main",
        max_summaries=10,
    )
    branch_ids = {s["session_id"] for s in out_main["summaries"]}
    assert "sess-A" in branch_ids
    assert "sess-C" not in branch_ids
    assert out_main["task_id"] == ctx["task_id"]


def test_handoff_task_id_path_unchanged(tmp_path: Path) -> None:
    """Direct task_id lookup should bypass the repo JOIN and behave as before."""
    parquet_dir, duckdb_path = _seed(tmp_path)
    ctx = _populate(parquet_dir, duckdb_path)
    out = drover_handoff(duckdb_path=duckdb_path, task_id=ctx["task_id"])
    assert out["task_id"] == ctx["task_id"]
    assert any("Plan 1" in s["summary_md"] for s in out["summaries"])


def test_session_replay_returns_recent_turns(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _populate(parquet_dir, duckdb_path)

    out = drover_session_replay(
        duckdb_path=duckdb_path, session_id="sess-A", last_n_turns=10
    )
    assert out["session_id"] == "sess-A"
    assert len(out["events"]) >= 1
    # Latest event first
    assert out["events"][0]["timestamp"] >= out["events"][-1]["timestamp"]


def test_session_replay_filters_empty_metadata_by_default(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    now = datetime.now(timezone.utc)
    tid = compute_task_id(None, "arniesaha", "nexus", "main")
    _write_agent_events(
        parquet_dir,
        [
            dict(
                id="real-user",
                session_id="sess-replay",
                agent_id="macmini-claude",
                task_id=tid,
                timestamp=now - timedelta(minutes=3),
                event_type="user_message",
                role="user",
                content="implement the recall fix",
                repo_owner="arniesaha",
                repo_name="nexus",
                branch="main",
                principal_id="arnab",
                dedup_key="real-user",
                raw_data="{}",
            ),
            dict(
                id="real-assistant",
                session_id="sess-replay",
                agent_id="macmini-claude",
                task_id=tid,
                timestamp=now - timedelta(minutes=2),
                event_type="assistant_message",
                role="assistant",
                content="done with tests and implementation",
                repo_owner="arniesaha",
                repo_name="nexus",
                branch="main",
                principal_id="arnab",
                dedup_key="real-assistant",
                raw_data="{}",
            ),
            dict(
                id="empty-title",
                session_id="sess-replay",
                agent_id="macmini-claude",
                task_id=tid,
                timestamp=now - timedelta(minutes=1),
                event_type="ai-title",
                role=None,
                content="",
                repo_owner="arniesaha",
                repo_name="nexus",
                branch="main",
                principal_id="arnab",
                dedup_key="empty-title",
                raw_data="{}",
            ),
            dict(
                id="empty-last-prompt",
                session_id="sess-replay",
                agent_id="macmini-claude",
                task_id=tid,
                timestamp=now,
                event_type="last-prompt",
                role=None,
                content=None,
                repo_owner="arniesaha",
                repo_name="nexus",
                branch="main",
                principal_id="arnab",
                dedup_key="empty-last-prompt",
                raw_data="{}",
            ),
        ],
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    out = drover_session_replay(
        duckdb_path=duckdb_path, session_id="sess-replay", last_n_turns=10
    )

    assert [event["content"] for event in out["events"]] == [
        "done with tests and implementation",
        "implement the recall fix",
    ]


def test_session_replay_can_include_empty_metadata_when_requested(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    now = datetime.now(timezone.utc)
    _write_agent_events(
        parquet_dir,
        [
            dict(
                id="empty-title",
                session_id="sess-replay-all",
                agent_id="macmini-claude",
                task_id="task",
                timestamp=now,
                event_type="ai-title",
                role=None,
                content="",
                repo_owner="arniesaha",
                repo_name="nexus",
                branch="main",
                principal_id="arnab",
                dedup_key="empty-title-all",
                raw_data="{}",
            )
        ],
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    out = drover_session_replay(
        duckdb_path=duckdb_path,
        session_id="sess-replay-all",
        last_n_turns=10,
        include_empty=True,
    )

    assert [event["event_type"] for event in out["events"]] == ["ai-title"]


def test_session_summary_returns_one_row(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _populate(parquet_dir, duckdb_path)
    out = drover_session_summary(duckdb_path=duckdb_path, session_id="sess-A")
    assert out is not None
    assert "Plan 1" in out["summary_md"]


def test_session_summary_returns_none_for_unknown(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _populate(parquet_dir, duckdb_path)
    assert drover_session_summary(duckdb_path=duckdb_path, session_id="nope") is None


def test_active_sessions_list(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    ctx = _populate(parquet_dir, duckdb_path)
    out = drover_active_sessions(duckdb_path=duckdb_path, task_id=ctx["task_id"])
    assert any(s["session_id"] == "sess-B" for s in out["active_sessions"])


def test_search_returns_one_row_per_canonical_logical_event(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    now = datetime(2026, 5, 22, 12, tzinfo=timezone.utc)
    rows = [
        dict(
            id="evt-source",
            session_id="sess-identity",
            agent_id="agent-a",
            task_id="task-a",
            timestamp=now,
            event_type="user_message",
            role="user",
            content="needle same logical event",
            repo_owner="arniesaha",
            repo_name="nexus",
            branch="main",
            principal_id="arnab",
            dedup_key="logical-a",
            raw_data="{}",
        ),
        dict(
            id="evt-source",
            session_id="sess-identity",
            agent_id="agent-a",
            task_id="task-a",
            timestamp=now + timedelta(seconds=1),
            event_type="user_message",
            role="user",
            content="needle same logical event normalized",
            repo_owner="arniesaha",
            repo_name="nexus",
            branch="main",
            principal_id="arnab",
            dedup_key="logical-a",
            raw_data="{}",
        ),
        dict(
            id="evt-source",
            session_id="sess-identity",
            agent_id="agent-a",
            task_id="task-a",
            timestamp=now + timedelta(seconds=2),
            event_type="assistant_message",
            role="assistant",
            content="needle distinct logical event sharing source id",
            repo_owner="arniesaha",
            repo_name="nexus",
            branch="main",
            principal_id="arnab",
            dedup_key="logical-b",
            raw_data="{}",
        ),
    ]
    _write_agent_events(parquet_dir, rows)
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    out = drover_search(duckdb_path=duckdb_path, query="needle", default_since_days=0)

    assert [row["content"] for row in out["results"]] == [
        "needle distinct logical event sharing source id",
        "needle same logical event normalized",
    ]


def test_search_finds_by_content(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _populate(parquet_dir, duckdb_path)
    out = drover_search(duckdb_path=duckdb_path, query="lakehouse rewrite", limit=10)
    assert any("lakehouse rewrite" in r["content"] for r in out["results"])


def test_search_defaults_to_recent_bounded_window_when_unscoped(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    now = datetime.now(timezone.utc)
    _write_agent_events(
        parquet_dir,
        [
            dict(
                id="old-hit",
                session_id="old-session",
                agent_id="agent-a",
                task_id="task-old",
                timestamp=now - timedelta(days=120),
                event_type="user_message",
                role="user",
                content="needle from stale history",
                repo_owner=None,
                repo_name=None,
                branch=None,
                principal_id="arnab",
                dedup_key="old-hit",
                raw_data="{}",
            ),
            dict(
                id="recent-hit",
                session_id="recent-session",
                agent_id="agent-a",
                task_id="task-recent",
                timestamp=now - timedelta(days=1),
                event_type="user_message",
                role="user",
                content="needle from recent history",
                repo_owner=None,
                repo_name=None,
                branch=None,
                principal_id="arnab",
                dedup_key="recent-hit",
                raw_data="{}",
            ),
        ],
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    out = drover_search(duckdb_path=duckdb_path, query="needle", limit=10)

    assert out["scoped"] is False
    assert out["default_since_days"] == 30
    assert [r["content"] for r in out["results"]] == ["needle from recent history"]


def test_files_touched_pulls_from_tool_use_blocks(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    ctx = _populate(parquet_dir, duckdb_path)
    out = drover_files_touched(duckdb_path=duckdb_path, task_id=ctx["task_id"])
    files = set(out["files"])
    assert "src/foo.py" in files


def test_task_status_aggregates(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    ctx = _populate(parquet_dir, duckdb_path)
    out = drover_task_status(duckdb_path=duckdb_path, task_id=ctx["task_id"])
    assert out["task_id"] == ctx["task_id"]
    assert out["session_count"] >= 2
    assert out["repo_owner"] == "arniesaha"


def test_handoff_empty_lakehouse_returns_well_formed(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    out = drover_handoff(
        duckdb_path=duckdb_path,
        repo_owner="nobody",
        repo_name="nothing",
        branch="never",
    )
    assert out["task_id"]  # still computes
    assert out["summaries"] == []
    assert out["active_sessions"] == []


def test_data_quality_returns_structured_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    duckdb_path = tmp_path / "nexus.duckdb"
    incoming_dir = tmp_path / "incoming"
    calls = []

    def fake_quality_snapshot(**kwargs: object) -> dict:
        calls.append(kwargs)
        return {
            "status": "warn",
            "score": 0.8,
            "categories": {
                "freshness": {
                    "status": "warn",
                    "score": 0.5,
                    "details": {"latest_event_age_hours": 7.5},
                    "warnings": ["latest agent_event is stale"],
                }
            },
            "warnings": ["freshness: latest agent_event is stale"],
        }

    monkeypatch.setattr(mcp_tools, "quality_snapshot", fake_quality_snapshot)

    out = mcp_tools.drover_data_quality(
        duckdb_path=duckdb_path, incoming_dir=incoming_dir, hours=12
    )

    assert calls == [
        {
            "duckdb_path": duckdb_path,
            "incoming_dir": incoming_dir,
            "hours": 12,
            "deep": False,
        }
    ]
    assert out["status"] == "warn"
    assert out["score"] == 0.8
    assert "freshness" in out["categories"]
    assert out["warnings"] == ["freshness: latest agent_event is stale"]
    json.dumps(out)
