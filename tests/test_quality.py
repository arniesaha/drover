"""Tests for Drover data-quality snapshots and Prometheus output."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from drover.schema import bootstrap
from drover.server.__main__ import main
from drover.server.quality import (
    FRESH_EVENT_CRITICAL_HOURS,
    format_prometheus,
    quality_snapshot,
)

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
        ("cost_usd", pa.float64()),
        ("dedup_key", pa.string()),
    ]
)


def _write_events(parquet_dir: Path, rows: list[dict]) -> None:
    by_agent: dict[str, list[dict]] = {}
    for row in rows:
        by_agent.setdefault(row["agent_id"], []).append(row)
    for agent_id, agent_rows in by_agent.items():
        date = agent_rows[0]["timestamp"].date().isoformat()
        out = parquet_dir / "agent_events" / f"date={date}" / f"agent_id={agent_id}"
        out.mkdir(parents=True, exist_ok=True)
        cols = {
            field.name: pa.array(
                [row.get(field.name) for row in agent_rows], type=field.type
            )
            for field in _EVENT_SCHEMA
        }
        pq.write_table(pa.table(cols, schema=_EVENT_SCHEMA), out / "part.parquet")


def _write_span(parquet_dir: Path, row: dict) -> None:
    date = row["start_time"].date().isoformat()
    out = parquet_dir / "spans" / f"date={date}"
    out.mkdir(parents=True, exist_ok=True)
    cols = {
        field.name: pa.array([row.get(field.name)], type=field.type)
        for field in _SPAN_SCHEMA
    }
    pq.write_table(pa.table(cols, schema=_SPAN_SCHEMA), out / "part.parquet")


def _seed_lakehouse(tmp_path: Path, *, degraded: bool) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    incoming = tmp_path / "incoming"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    now = datetime.now(timezone.utc)
    event_rows = [
        {
            "id": "evt-1",
            "session_id": "s1",
            "agent_id": "agent-a",
            "task_id": "t1",
            "timestamp": now - timedelta(minutes=8),
            "event_type": "UserPromptSubmit",
            "role": "user",
            "content": "start the handoff",
            "repo_owner": "arniesaha",
            "repo_name": "nexus",
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "dedup-1",
            "raw_data": '{"cwd":"/Users/arnabmac/jenny/nexus"}',
        },
        {
            "id": "evt-2" if not degraded else "evt-1",
            "session_id": "s2",
            "agent_id": "agent-a",
            "task_id": "t2",
            "timestamp": now - timedelta(minutes=4),
            "event_type": "Stop",
            "role": "assistant",
            "content": "done",
            "repo_owner": "arniesaha" if not degraded else None,
            "repo_name": "nexus" if not degraded else None,
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "dedup-2" if not degraded else "dedup-1",
            "raw_data": '{"cwd":"/tmp/unknown"}',
        },
    ]
    _write_events(parquet_dir, event_rows)
    _write_span(
        parquet_dir,
        {
            "trace_id": "trace-1",
            "span_id": "span-1",
            "parent_span_id": None,
            "name": "llm_call",
            "service_name": "claude-code",
            "start_time": now - timedelta(minutes=3),
            "end_time": now - timedelta(minutes=2),
            "duration_ms": 60_000.0,
            "session_id": "s1",
            "task_id": "t1",
            "agent_id": "agent-a",
            "cost_usd": 0.01,
            "dedup_key": "span-dedup-1",
        },
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO tasks (task_id, repo_owner, repo_name, branch, status, title) VALUES ('t1', 'arniesaha', 'nexus', 'main', 'active', 'quality')"
        )
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts, last_error, updated_at) VALUES ('s1', 'done', 1, NULL, now())"
        )
        con.execute(
            "INSERT INTO embed_jobs (session_id, status, attempts, last_error, updated_at) VALUES ('s1', ?, 0, ?, now())",
            ["pending" if degraded else "done", "embed offline" if degraded else None],
        )
        if not degraded:
            con.execute("""INSERT INTO session_summaries
                   (session_id, task_id, agent_id, ended_at, summary_md,
                    files_touched, tools_used, last_user_prompt, last_assistant,
                    next_steps_md, open_questions, status, generator_model, generated_at)
                   VALUES (
                    's1', 't1', 'agent-a', now(), 'Summary',
                    ['src/drover/server/quality.py'], MAP {'duckdb': 1},
                    'please summarize the latest run', 'captured the summary bundle',
                    'run the quality verification suite', ['Should we widen the window?'],
                    'complete', 'test-model', now()
                   )""")
            con.execute("""INSERT INTO session_summaries
                   (session_id, task_id, agent_id, ended_at, summary_md,
                    files_touched, tools_used, last_user_prompt, last_assistant,
                    next_steps_md, open_questions, status, generator_model, generated_at)
                   VALUES (
                    's2', 't1', 'agent-a', now(), 'Second summary',
                    ['tests/test_quality.py'], MAP {'pytest': 1},
                    'confirm the degraded path', 'recorded the second bundle',
                    'watch the next embedding run', ['Do we need a wider fixture?'],
                    'complete', 'test-model', now()
                   )""")
            con.execute(
                "INSERT INTO session_embeddings (session_id, embedding, model, dim, embedded_at) VALUES ('s1', [0.1, 0.2], 'm', 2, now())"
            )
            con.execute(
                "INSERT INTO session_embeddings (session_id, embedding, model, dim, embedded_at) VALUES ('s2', [0.3, 0.4], 'm', 2, now())"
            )
            con.execute("""INSERT INTO span_embeddings
                   (span_id, trace_id, session_id, task_id, agent_id, repo_owner, repo_name,
                    branch, source_text, source_fields, embedding, model, dim, embedded_at)
                   VALUES (
                    'span-1', 'trace-1', 's1', 't1', 'agent-a', 'arniesaha', 'nexus',
                    'main', 'llm_call', ['name'], [0.5, 0.6], 'm', 2, now()
                   )""")
    finally:
        con.close()

    if degraded:
        (incoming / "agent-a").mkdir(parents=True)
        (incoming / "agent-a" / "stuck.jsonl").write_text("{}\n")
    return duckdb_path, incoming


def test_quality_snapshot_reports_healthy_categories(tmp_path: Path) -> None:
    duckdb_path, incoming = _seed_lakehouse(tmp_path, degraded=False)

    snapshot = quality_snapshot(duckdb_path=duckdb_path, incoming_dir=incoming)

    assert snapshot["status"] == "ok"
    assert snapshot["score"] == 1.0
    assert set(snapshot["categories"]) == {
        "freshness",
        "completeness",
        "summary_coverage",
        "embedding_coverage",
        "attribution",
        "bundle_quality",
        "identity",
        "derived_context",
        "agent_adoption",
    }
    for category in snapshot["categories"].values():
        assert category["status"] == "ok"
        assert category["score"] == 1.0
    assert snapshot["categories"]["derived_context"]["details"]["handoff_ready"] == 1


def test_quality_snapshot_reports_degraded_categories(tmp_path: Path) -> None:
    duckdb_path, incoming = _seed_lakehouse(tmp_path, degraded=True)

    snapshot = quality_snapshot(duckdb_path=duckdb_path, incoming_dir=incoming)

    assert snapshot["status"] == "critical"
    assert snapshot["categories"]["freshness"]["status"] == "warn"
    assert snapshot["categories"]["attribution"]["status"] == "ok"
    assert snapshot["categories"]["summary_coverage"]["status"] == "critical"
    assert snapshot["categories"]["embedding_coverage"]["status"] == "critical"
    assert snapshot["categories"]["bundle_quality"]["status"] == "critical"
    assert snapshot["categories"]["identity"]["status"] == "critical"
    assert snapshot["categories"]["derived_context"]["status"] == "critical"
    assert (
        snapshot["categories"]["identity"]["details"]["duplicate_dedup_key_values"] == 1
    )
    assert snapshot["categories"]["derived_context"]["details"]["handoff_ready"] == 0
    assert any("duplicate dedup_key" in warning for warning in snapshot["warnings"])


def test_bundle_quality_uses_recall_usable_not_rich_metadata(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            """INSERT INTO session_summaries
               (session_id, task_id, agent_id, ended_at, summary_md,
                files_touched, tools_used, last_user_prompt, last_assistant,
                next_steps_md, open_questions, status, generator_model, generated_at)
               VALUES
               ('historical-completed', 't1', 'agent-a', now(), 'Useful historical summary',
                NULL, MAP {}, NULL, NULL, NULL, NULL, 'completed', 'old-model', now())"""
        )
        con.execute(
            "INSERT INTO session_embeddings (session_id, embedding, model, dim, embedded_at) VALUES ('historical-completed', [0.1, 0.2], 'm', 2, now())"
        )
    finally:
        con.close()

    snapshot = quality_snapshot(duckdb_path=duckdb_path)
    category = snapshot["categories"]["bundle_quality"]
    details = category["details"]

    assert category["status"] == "ok"
    assert details["complete_summaries"] == 1
    assert details["recall_usable_summaries"] == 1
    assert details["recall_usable_percent"] == 100.0
    assert details["bundle_ready_summaries"] == 0
    assert details["bundle_ready_percent"] == 0.0
    assert details["missing_recall_processing_summaries"] == 0
    assert details["missing_rich_evidence_summaries"] == 1
    assert not any("bundle-ready summaries cover" in w for w in snapshot["warnings"])

    prometheus = format_prometheus(snapshot)
    assert "drover_quality_bundle_recall_usable_percent 100.0" in prometheus
    assert (
        'drover_quality_bundle_missing_summaries{kind="rich_evidence"} 1' in prometheus
    )


def test_bundle_quality_warns_on_missing_processing_not_missing_evidence(
    tmp_path: Path,
) -> None:
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("""INSERT INTO session_summaries
               (session_id, task_id, agent_id, ended_at, summary_md,
                files_touched, tools_used, last_user_prompt, last_assistant,
                next_steps_md, open_questions, status, generator_model, generated_at)
               VALUES
               ('embedded', 't1', 'agent-a', now(), 'Embedded summary',
                NULL, MAP {}, NULL, NULL, NULL, NULL, 'complete', 'm', now()),
               ('not-embedded', 't1', 'agent-a', now(), 'Summary without embedding',
                NULL, MAP {}, NULL, NULL, NULL, NULL, 'complete', 'm', now())""")
        con.execute(
            "INSERT INTO session_embeddings (session_id, embedding, model, dim, embedded_at) VALUES ('embedded', [0.1, 0.2], 'm', 2, now())"
        )
    finally:
        con.close()

    snapshot = quality_snapshot(duckdb_path=duckdb_path)
    category = snapshot["categories"]["bundle_quality"]

    assert category["status"] == "warn"
    assert category["details"]["recall_usable_summaries"] == 1
    assert category["details"]["missing_recall_processing_summaries"] == 1
    assert category["details"]["missing_rich_evidence_summaries"] == 2
    assert any("recall-usable summaries cover 1/2" in w for w in snapshot["warnings"])


def test_freshness_uses_stalest_agent_latest_event(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    now = datetime.now(timezone.utc)

    _write_events(
        parquet_dir,
        [
            {
                "id": "fresh-1",
                "session_id": "fresh-session",
                "agent_id": "agent-fresh",
                "task_id": "fresh-task",
                "timestamp": now - timedelta(minutes=5),
                "event_type": "Stop",
                "role": "assistant",
                "content": "fresh source",
                "repo_owner": "arniesaha",
                "repo_name": "nexus",
                "branch": "main",
                "principal_id": "arnab",
                "dedup_key": "fresh-dedup",
                "raw_data": "{}",
            },
            {
                "id": "stale-1",
                "session_id": "stale-session",
                "agent_id": "agent-stale",
                "task_id": "stale-task",
                "timestamp": now - timedelta(hours=FRESH_EVENT_CRITICAL_HOURS + 1),
                "event_type": "Stop",
                "role": "assistant",
                "content": "stale source",
                "repo_owner": "arniesaha",
                "repo_name": "nexus",
                "branch": "main",
                "principal_id": "arnab",
                "dedup_key": "stale-dedup",
                "raw_data": "{}",
            },
        ],
    )
    _write_span(
        parquet_dir,
        {
            "trace_id": "trace-fresh",
            "span_id": "span-fresh",
            "parent_span_id": None,
            "name": "llm_call",
            "service_name": "claude-code",
            "start_time": now - timedelta(minutes=2),
            "end_time": now - timedelta(minutes=1),
            "duration_ms": 60_000.0,
            "session_id": "fresh-session",
            "task_id": "fresh-task",
            "agent_id": "agent-fresh",
            "cost_usd": 0.01,
            "dedup_key": "span-fresh-dedup",
        },
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    snapshot = quality_snapshot(
        duckdb_path=duckdb_path, now=now, required_agent_ids={"agent-stale"}
    )
    freshness = snapshot["categories"]["freshness"]

    assert freshness["status"] == "critical"
    assert freshness["details"]["oldest_latest_event_age_hours"] > (
        FRESH_EVENT_CRITICAL_HOURS
    )
    assert "agent-stale" in " ".join(freshness["warnings"])


def _quality_audit_stub(
    now: datetime,
    *,
    stale_agent_hours: int = 72,
    extra_latest_events: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    latest_events = {
        "agent-fresh": {
            "timestamp": (now - timedelta(minutes=5)).isoformat(),
            "event_type": "Stop",
            "session_id": "fresh-session",
            "repo": "arniesaha/nexus",
        },
        "agent-idle": {
            "timestamp": (now - timedelta(hours=stale_agent_hours)).isoformat(),
            "event_type": "Stop",
            "session_id": "idle-session",
            "repo": None,
        },
    }
    latest_events.update(extra_latest_events or {})
    return {
        "warnings": [
            "agent_events has duplicate id values; use documented dedup_key semantics for logical event uniqueness"
        ],
        "latest_events": latest_events,
        "span_health": {
            "status": "ok",
            "latest_start": (now - timedelta(minutes=2)).isoformat(),
            "recent_count": 10,
        },
        "unprocessed_incoming": [],
        "table_counts": {
            "agent_events": 1000,
            "tasks": 1,
            "session_summaries": 10,
            "summarize_jobs": 10,
            "embed_jobs": 10,
            "session_embeddings": 10,
            "span_embed_jobs": 10,
            "span_embeddings": 10,
            "spans": 10,
        },
        "summarize_jobs": {"status_counts": {"done": 10}, "recent_errors": []},
        "embed_jobs": {"status_counts": {"done": 10}, "recent_errors": []},
        "session_embeddings_count": 10,
        "span_embedding_coverage": {
            "embedded_spans": 10,
            "embedded_recent_spans": 10,
            "pending_jobs": 0,
            "stale_running_jobs": 0,
            "total_recent_spans": 10,
            "coverage_percent": 100.0,
            "coverage_note": None,
        },
        "bundle_quality": {
            "total_summaries": 10,
            "summaries_with_summary_md": 10,
            "summaries_with_next_steps_md": 10,
            "summaries_with_files_touched": 10,
            "summaries_with_open_questions": 10,
            "summaries_with_last_user_prompt": 10,
            "summaries_with_last_assistant": 10,
            "summaries_with_generator_model": 10,
            "complete_summaries": 10,
            "bundle_ready_summaries": 10,
            "bundle_ready_percent": 100.0,
        },
        "embedding_status": {"state": "idle", "message": "no pending embed jobs"},
        "session_consistency": {"status": "ok"},
        "agent_event_identity": {
            "status": "ok",
            "canonical_semantics": "dedup_key",
            "duplicate_id_values": 50,
            "duplicate_id_rows": 75,
            "duplicate_dedup_key_values": 0,
            "duplicate_dedup_key_rows": 0,
        },
        "repo_attribution": {
            "agent-fresh": {"total": 500, "attributed": 475, "percent": 95.0},
            "agent-tiny": {"total": 10, "attributed": 0, "percent": 0.0},
        },
        "top_unattributed_cwds": {
            "24h": [{"agent_id": "agent-tiny", "cwd": "<missing>", "count": 10}],
            "7d": [],
        },
    }


def test_quality_reports_openclaw_agentweave_contract_health_category(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    audit = _quality_audit_stub(now)
    audit["openclaw_agentweave_health"] = {
        "status": "warn",
        "native_events": {
            "total": 24573,
            "raw_harness_openclaw": 0,
            "raw_session_key_present": 0,
            "raw_session_uuid_present": 0,
            "unknown_openclaw_session_rows": 23330,
        },
        "spans": {
            "openclaw_like_spans": 37,
            "column_harness_openclaw": 1,
            "attr_harness_openclaw": 37,
            "attr_openclaw_but_column_harness_null": 36,
            "attr_session_key_but_column_null": 36,
        },
        "linkability": {
            "openclaw_like_spans": 37,
            "exact_session_id_matches": 0,
            "session_key_matches": 0,
            "unmatched_spans": 37,
        },
    }
    monkeypatch.setattr("drover.server.quality.runtime_audit", lambda **_: audit)

    snapshot = quality_snapshot(duckdb_path=tmp_path / "drover.duckdb", now=now)

    category = snapshot["categories"]["openclaw_agentweave"]
    linkability = snapshot["categories"]["span_linkability"]
    assert snapshot["status"] == "warn"
    assert category["status"] == "warn"
    assert linkability["status"] == "warn"
    assert category["details"]["linkability"]["unmatched_spans"] == 37
    assert linkability["details"]["unmatched_spans"] == 37
    assert any("OpenClaw/AgentWeave" in warning for warning in snapshot["warnings"])


def test_quality_calibrates_documented_duplicate_ids_and_low_volume_attribution(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "drover.server.quality.runtime_audit",
        lambda **_: _quality_audit_stub(now),
    )

    snapshot = quality_snapshot(duckdb_path=tmp_path / "drover.duckdb", now=now)

    assert snapshot["status"] == "warn"
    assert snapshot["categories"]["identity"]["status"] == "ok"
    assert snapshot["categories"]["attribution"]["status"] == "warn"
    assert snapshot["categories"]["summary_coverage"]["status"] == "ok"
    assert snapshot["categories"]["embedding_coverage"]["status"] == "ok"
    assert snapshot["categories"]["bundle_quality"]["status"] == "ok"
    assert snapshot["categories"]["derived_context"]["status"] == "ok"
    assert any("low-volume" in warning for warning in snapshot["warnings"])
    assert not any(
        "dedup_key remains canonical" in warning for warning in snapshot["warnings"]
    )


def test_quality_treats_standard_attribution_skip_as_info(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    audit = _quality_audit_stub(now, stale_agent_hours=1)
    audit["warnings"] = []
    audit["repo_attribution"] = {}
    audit["skipped_checks"] = [
        "repo attribution and cwd gap diagnostics require deep mode"
    ]
    monkeypatch.setattr("drover.server.quality.runtime_audit", lambda **_: audit)

    snapshot = quality_snapshot(
        duckdb_path=tmp_path / "drover.duckdb", now=now, deep=False
    )
    attribution = snapshot["categories"]["attribution"]

    assert snapshot["status"] == "ok"
    assert snapshot["score"] == 1.0
    assert attribution["status"] == "ok"
    assert attribution["warnings"] == []
    assert attribution["details"]["repo_attribution_skipped"] is True
    assert attribution["details"]["repo_attribution_evaluation"] == "skipped"
    assert not any("attribution:" in warning for warning in snapshot["warnings"])


def test_quality_treats_sparse_terminal_summary_errors_as_warning(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    audit = _quality_audit_stub(now)
    audit["session_consistency"] = {
        "status": "drift",
        "event_sessions": 1794,
        "event_sessions_without_summary": 6,
        "summaries_without_events": 0,
    }
    audit["summarize_jobs"] = {
        "status_counts": {"done": 1788, "errored": 3},
        "recent_errors": [],
        "backend_health": {
            "state": "errors",
            "pending": 0,
            "running": 0,
            "errored": 3,
            "retryable_errors": 0,
            "non_retryable_errors": 3,
            "error_categories": {"validation": 3},
        },
    }
    monkeypatch.setattr("drover.server.quality.runtime_audit", lambda **_: audit)

    snapshot = quality_snapshot(duckdb_path=tmp_path / "drover.duckdb", now=now)

    category = snapshot["categories"]["summary_coverage"]
    assert snapshot["status"] == "warn"
    assert category["status"] == "warn"
    assert category["details"]["coverage_percent"] == 99.7
    assert category["details"]["retryable_summarize_errors"] == 0
    assert category["details"]["non_retryable_summarize_errors"] == 3


def test_quality_keeps_retryable_summary_errors_critical(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    audit = _quality_audit_stub(now)
    audit["session_consistency"] = {
        "status": "drift",
        "event_sessions": 1794,
        "event_sessions_without_summary": 6,
        "summaries_without_events": 0,
    }
    audit["summarize_jobs"] = {
        "status_counts": {"done": 1788, "errored": 3},
        "recent_errors": [],
        "backend_health": {
            "state": "retryable_errors",
            "pending": 0,
            "running": 0,
            "errored": 3,
            "retryable_errors": 3,
            "non_retryable_errors": 0,
            "error_categories": {"runtime": 3},
        },
    }
    monkeypatch.setattr("drover.server.quality.runtime_audit", lambda **_: audit)

    snapshot = quality_snapshot(duckdb_path=tmp_path / "drover.duckdb", now=now)

    assert snapshot["status"] == "critical"
    assert snapshot["categories"]["summary_coverage"]["status"] == "critical"


def test_quality_keeps_high_volume_zero_attribution_critical(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    audit = _quality_audit_stub(now)
    audit["repo_attribution"] = {
        "agent-fresh": {"total": 500, "attributed": 475, "percent": 95.0},
        "agent-broken": {"total": 250, "attributed": 0, "percent": 0.0},
    }
    monkeypatch.setattr("drover.server.quality.runtime_audit", lambda **_: audit)

    snapshot = quality_snapshot(duckdb_path=tmp_path / "drover.duckdb", now=now)

    assert snapshot["status"] == "critical"
    assert snapshot["categories"]["attribution"]["status"] == "critical"
    assert any("high-volume" in warning for warning in snapshot["warnings"])


def test_quality_excludes_general_workspace_from_attribution_failures(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    audit = _quality_audit_stub(now)
    audit["repo_attribution"] = {
        "nas-openclaw": {
            "total": 250,
            "attributed": 0,
            "general_workspace": 250,
            "project_total": 0,
            "percent": 100.0,
        }
    }
    audit["top_unattributed_cwds"] = {"24h": [], "7d": []}
    audit["general_workspace_cwds"] = {
        "24h": [{"agent_id": "nas-openclaw", "cwd": "/home/Arnab", "count": 250}],
        "7d": [],
    }
    monkeypatch.setattr("drover.server.quality.runtime_audit", lambda **_: audit)

    snapshot = quality_snapshot(duckdb_path=tmp_path / "drover.duckdb", now=now)
    attribution = snapshot["categories"]["attribution"]

    assert attribution["status"] == "ok"
    assert attribution["details"]["general_workspace_events"] == 250
    assert attribution["details"]["project_events"] == 0
    assert attribution["details"]["high_volume_zero_attribution_agents"] == []
    assert not any("high-volume" in warning for warning in snapshot["warnings"])


def test_quality_treats_stale_idle_sources_as_warning_by_default(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "drover.server.quality.runtime_audit",
        lambda **_: _quality_audit_stub(now, stale_agent_hours=72),
    )

    snapshot = quality_snapshot(duckdb_path=tmp_path / "drover.duckdb", now=now)

    assert snapshot["categories"]["freshness"]["status"] == "warn"
    assert snapshot["status"] == "warn"
    assert any("idle source" in warning for warning in snapshot["warnings"])


def test_quality_keeps_stale_required_sources_critical(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "drover.server.quality.runtime_audit",
        lambda **_: _quality_audit_stub(now, stale_agent_hours=72),
    )

    snapshot = quality_snapshot(
        duckdb_path=tmp_path / "drover.duckdb",
        now=now,
        required_agent_ids={"agent-idle"},
    )

    assert snapshot["status"] == "critical"
    assert snapshot["categories"]["freshness"]["status"] == "critical"
    assert any("required source" in warning for warning in snapshot["warnings"])


def test_quality_does_not_mask_stale_required_source_behind_older_idle_source(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "drover.server.quality.runtime_audit",
        lambda **_: _quality_audit_stub(
            now,
            stale_agent_hours=72,
            extra_latest_events={
                "agent-required": {
                    "timestamp": (now - timedelta(hours=25)).isoformat(),
                    "event_type": "Stop",
                    "session_id": "required-session",
                    "repo": "arniesaha/nexus",
                }
            },
        ),
    )

    snapshot = quality_snapshot(
        duckdb_path=tmp_path / "drover.duckdb",
        now=now,
        required_agent_ids={"agent-required"},
    )

    assert snapshot["status"] == "critical"
    assert snapshot["categories"]["freshness"]["status"] == "critical"
    assert any("agent-required" in warning for warning in snapshot["warnings"])


def test_quality_reports_absent_required_sources_as_critical(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "drover.server.quality.runtime_audit",
        lambda **_: _quality_audit_stub(now),
    )

    snapshot = quality_snapshot(
        duckdb_path=tmp_path / "drover.duckdb",
        now=now,
        required_agent_ids={"agent-missing"},
    )

    assert snapshot["status"] == "critical"
    assert snapshot["categories"]["freshness"]["status"] == "critical"
    assert any("missing required source" in warning for warning in snapshot["warnings"])


def test_quality_required_agent_env_marks_stale_source_critical(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("DROVER_QUALITY_REQUIRED_AGENTS", "agent-idle")
    monkeypatch.setattr(
        "drover.server.quality.runtime_audit",
        lambda **_: _quality_audit_stub(now, stale_agent_hours=72),
    )

    snapshot = quality_snapshot(duckdb_path=tmp_path / "drover.duckdb", now=now)

    assert snapshot["status"] == "critical"
    assert snapshot["categories"]["freshness"]["status"] == "critical"


def test_quality_uses_attributed_count_for_zero_attribution_classification(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    audit = _quality_audit_stub(now)
    audit["repo_attribution"] = {
        "agent-fresh": {"total": 500, "attributed": 475, "percent": 95.0},
        "agent-low-but-nonzero": {"total": 10000, "attributed": 1, "percent": 0.0},
    }
    monkeypatch.setattr("drover.server.quality.runtime_audit", lambda **_: audit)

    snapshot = quality_snapshot(duckdb_path=tmp_path / "drover.duckdb", now=now)

    assert snapshot["status"] == "warn"
    assert snapshot["categories"]["attribution"]["status"] == "warn"
    assert not snapshot["categories"]["attribution"]["details"][
        "high_volume_zero_attribution_agents"
    ]


def test_quality_prometheus_output_has_stable_metric_names(tmp_path: Path) -> None:
    duckdb_path, incoming = _seed_lakehouse(tmp_path, degraded=True)
    snapshot = quality_snapshot(duckdb_path=duckdb_path, incoming_dir=incoming)

    text = format_prometheus(snapshot)

    assert "# HELP drover_quality_score" in text
    assert 'drover_quality_score{category="overall"}' in text
    assert 'drover_quality_status{category="overall",status="ok"} 0' in text
    assert 'drover_quality_status{category="overall",status="warn"} 0' in text
    assert 'drover_quality_status{category="overall",status="critical"} 1' in text
    assert 'drover_quality_status{category="overall",status="unknown"} 0' in text
    assert 'drover_quality_status{category="identity",status="critical"} 1' in text
    assert 'drover_quality_table_rows{table="agent_events"} 2' in text
    assert "drover_quality_identity_duplicate_dedup_key_values 1" in text
    assert "drover_quality_handoff_ready 0" in text
    assert 'drover_quality_repo_attribution_percent{agent_id="agent-a"} 100.0' in text
    assert "drover_quality_unprocessed_incoming_files 1" in text


def test_cli_quality_emits_json_and_prometheus(tmp_path: Path) -> None:
    duckdb_path, incoming = _seed_lakehouse(tmp_path, degraded=False)
    runner = CliRunner()

    json_res = runner.invoke(
        main,
        [
            "quality",
            "--db",
            str(duckdb_path),
            "--incoming-dir",
            str(incoming),
            "--json",
        ],
    )
    prometheus_res = runner.invoke(
        main,
        [
            "quality",
            "--db",
            str(duckdb_path),
            "--incoming-dir",
            str(incoming),
            "--prometheus",
        ],
    )

    assert json_res.exit_code == 0, json_res.output
    assert json.loads(json_res.output)["categories"]["freshness"]["status"] == "ok"
    assert prometheus_res.exit_code == 0, prometheus_res.output
    assert "drover_quality_score" in prometheus_res.output
