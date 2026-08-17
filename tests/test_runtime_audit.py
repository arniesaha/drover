"""Tests for Drover runtime health audit helpers."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from drover.event_identity import audit_agent_event_identity, scan_agent_events_once
from drover.schema import bootstrap
from drover.server.__main__ import main
from drover.server.doctor import format_runtime_audit, runtime_audit

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
        ("attributes_json", pa.string()),
        ("dedup_key", pa.string()),
    ]
)


def _write_agent_events(parquet_dir: Path) -> None:
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
    now = datetime.now(timezone.utc)
    rows = {
        "id": ["a1", "a2", "a3", "b1", "old", "week", "c1", "c2", "c3", "c4"],
        "session_id": ["s1", "s2", "s2", "s3", "s4", "s5", "c1", "c2", "c3", "c4"],
        "agent_id": [
            "agent-a",
            "agent-a",
            "agent-a",
            "agent-b",
            "agent-b",
            "agent-a",
            "macmini-claude",
            "macmini-claude",
            "macmini-claude",
            "macmini-claude",
        ],
        "task_id": ["t1", "t2", "t-home", "t3", "t4", "t5", "tc1", "tc2", "tc3", "tc4"],
        "timestamp": [
            now - timedelta(minutes=30),
            now,
            now - timedelta(minutes=1),
            now - timedelta(minutes=5),
            now - timedelta(hours=48),
            now - timedelta(days=3),
            now - timedelta(minutes=10),
            now - timedelta(minutes=9),
            now - timedelta(minutes=8),
            now - timedelta(minutes=7),
        ],
        "event_type": [
            "UserPromptSubmit",
            "Stop",
            "Stop",
            "Notification",
            "Stop",
            "Stop",
            "Notification",
            "Notification",
            "Notification",
            "Notification",
        ],
        "role": [
            "user",
            "assistant",
            "assistant",
            "system",
            "assistant",
            "assistant",
            "system",
            "system",
            "system",
            "system",
        ],
        "content": [
            "one",
            "two",
            "home",
            "three",
            "old",
            "week",
            "c1",
            "c2",
            "c3",
            "c4",
        ],
        "repo_owner": [
            "acme",
            None,
            None,
            "acme",
            "old",
            "acme",
            None,
            None,
            None,
            None,
        ],
        "repo_name": [
            "repo",
            None,
            None,
            "repo",
            "repo",
            "week",
            None,
            None,
            None,
            None,
        ],
        "branch": ["main", None, None, "main", "main", "main", None, None, None, None],
        "principal_id": ["p", "p", "p", "p", "p", "p", "p", "p", "p", "p"],
        "dedup_key": [
            "k1",
            "k2",
            "k-home",
            "k3",
            "k4",
            "k5",
            "kc1",
            "kc2",
            "kc3",
            "kc4",
        ],
        "raw_data": [
            '{"cwd":"/project/attributed"}',
            '{"cwd":"/Users/arnabmac"}',
            '{"workspaceDir":"/home/Arnab"}',
            '{"workspaceDir":"/project/attributed-b"}',
            '{"cwd":"/old"}',
            '{"cwd":"/week/attributed"}',
            '{"type":"ai-title"}',
            '{"cwd":"/Users/arnabmac/.claude-mem/observer-sessions"}',
            '{"cwd":"/tmp/not-a-repo"}',
            "{not-json",
        ],
    }
    partitions = sorted(
        {
            (rows["agent_id"][i], rows["timestamp"][i].date().isoformat())
            for i in range(len(rows["id"]))
        }
    )
    for agent, date in partitions:
        idxs = [
            i
            for i, value in enumerate(rows["agent_id"])
            if value == agent and rows["timestamp"][i].date().isoformat() == date
        ]
        agent_rows = {k: [v[i] for i in idxs] for k, v in rows.items()}
        table = pa.table(
            {k: pa.array(v, type=schema.field(k).type) for k, v in agent_rows.items()},
            schema=schema,
        )
        out = parquet_dir / "agent_events" / f"date={date}" / f"agent_id={agent}"
        out.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, out / "part-test.parquet")


def _write_span(parquet_dir: Path, **row) -> None:
    date = row["start_time"].date().isoformat()
    out = parquet_dir / "spans" / f"date={date}"
    out.mkdir(parents=True, exist_ok=True)
    cols = {f.name: pa.array([row.get(f.name)], type=f.type) for f in _SPAN_SCHEMA}
    pq.write_table(
        pa.table(cols, schema=_SPAN_SCHEMA), out / f"{row['span_id']}.parquet"
    )


def _seed_runtime_db(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)
    _write_agent_events(parquet_dir)
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts, last_error, updated_at) VALUES ('s1', 'done', 1, NULL, now())"
        )
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts, last_error, updated_at) VALUES ('s2', 'errored', 2, 'boom', now())"
        )
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts, last_error, updated_at) VALUES ('s3', 'pending', 0, NULL, now())"
        )
        con.execute(
            "INSERT INTO embed_jobs (session_id, status, attempts, last_error, updated_at) VALUES ('s1', 'pending', 0, NULL, now())"
        )
        con.execute(
            "INSERT INTO embed_jobs (session_id, status, attempts, last_error, updated_at) VALUES ('s2', 'done', 1, NULL, now())"
        )
        con.execute(
            "INSERT INTO session_embeddings (session_id, embedding, model, dim, embedded_at) VALUES ('s2', [0.1, 0.2], 'm', 2, now())"
        )
        con.execute(
            "INSERT INTO span_embed_jobs (span_id, status, attempts, last_error, updated_at) VALUES ('span-1', 'pending', 0, NULL, now())"
        )
        con.execute("""INSERT INTO span_embeddings
               (span_id, trace_id, session_id, task_id, agent_id, source_text,
                source_fields, embedding, model, dim, embedded_at)
               VALUES ('span-2', 'trace-2', 'aw-s2', 'task', 'agent-a', 'prompt: ok',
                       ['prompt_preview'], [0.1, 0.2], 'm', 2, now())""")
    finally:
        con.close()
    incoming = tmp_path / "incoming"
    (incoming / "agent-a" / ".processed").mkdir(parents=True)
    (incoming / "agent-a").mkdir(parents=True, exist_ok=True)
    (incoming / "agent-a" / "stuck.jsonl").write_text("{}\n")
    (incoming / "agent-a" / ".processed" / "ok.jsonl").write_text("{}\n")
    return db, incoming


def test_runtime_audit_reports_operational_health(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "DROVER_GENERAL_WORKSPACE_ROOTS",
        "/Users/arnabmac:/home/Arnab:/Users/arnabmac/.claude-mem/observer-sessions",
    )
    db, incoming = _seed_runtime_db(tmp_path)

    report = runtime_audit(duckdb_path=db, incoming_dir=incoming, hours=24)

    assert report["table_counts"]["summarize_jobs"] == 3
    assert report["latest_events"]["agent-a"]["event_type"] == "Stop"
    assert report["summarize_jobs"]["status_counts"] == {
        "done": 1,
        "errored": 1,
        "pending": 1,
    }
    assert report["summarize_jobs"]["recent_errors"][0]["last_error"] == "boom"
    assert report["embed_jobs"]["status_counts"] == {"done": 1, "pending": 1}
    assert report["span_embed_jobs"]["status_counts"] == {"pending": 1}
    assert report["embedding_status"]["state"] == "backlog"
    assert "pending" in report["embedding_status"]["message"]
    assert report["session_embeddings_count"] == 1
    assert report["span_embedding_coverage"]["embedded_spans"] == 1
    assert report["span_embedding_coverage"]["pending_jobs"] == 1
    assert report["repo_attribution"]["agent-a"]["percent"] == 100.0
    assert report["repo_attribution"]["agent-a"]["general_workspace"] == 2
    assert report["repo_attribution"]["agent-a"]["project_total"] == 1
    assert report["repo_attribution"]["agent-b"]["percent"] == 100.0
    assert report["repo_attribution"]["macmini-claude"]["percent"] == 0.0
    assert report["repo_attribution"]["macmini-claude"]["general_workspace"] == 1
    assert report["repo_attribution_windows"]["24h"]["agent-a"]["percent"] == 100.0
    assert report["repo_attribution_windows"]["7d"]["agent-a"]["percent"] == 100.0
    assert report["top_unattributed_cwds"]["24h"][0] == {
        "agent_id": "macmini-claude",
        "cwd": "<missing>",
        "count": 2,
    }
    assert {
        "agent_id": "macmini-claude",
        "cwd": "/tmp/not-a-repo",
        "count": 1,
    } in report["top_unattributed_cwds"]["24h"]
    assert report["general_workspace_cwds"]["24h"][0] == {
        "agent_id": "agent-a",
        "cwd": "/Users/arnabmac",
        "count": 1,
    }
    assert report["claude_attribution_gap_categories"]["24h"]["macmini-claude"] == {
        "general_context_activity": {
            "count": 1,
            "samples": [
                {
                    "cwd": "/Users/arnabmac/.claude-mem/observer-sessions",
                    "count": 1,
                }
            ],
        },
        "genuine_unknown": {
            "count": 1,
            "samples": [{"cwd": "/tmp/not-a-repo", "count": 1}],
        },
        "missing_producer_metadata": {
            "count": 1,
            "samples": [{"cwd": "<missing>", "count": 1}],
        },
        "parser_collector_drift": {
            "count": 1,
            "samples": [{"cwd": "<missing>", "count": 1}],
        },
    }
    assert report["unprocessed_incoming"] == ["agent-a/stuck.jsonl"]
    assert report["agent_event_identity"]["duplicate_id_values"] == 0
    assert report["agent_event_identity"]["duplicate_id_rows"] == 0
    assert report["agent_event_identity"]["duplicate_dedup_key_values"] == 0
    assert report["agent_event_identity"]["duplicate_dedup_key_rows"] == 0
    assert report["session_consistency"]["status"] == "ok"
    assert report["session_consistency"]["sessions_relation_type"] == "VIEW"
    assert report["session_consistency"]["event_sessions_without_summary"] == 9


def test_runtime_audit_formats_source_and_diagnostic_path_labels(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "live.duckdb"
    diagnostic_db = tmp_path / "snapshot.duckdb"
    report = runtime_audit(
        duckdb_path=diagnostic_db,
        source_duckdb_path=source_db,
        diagnostic_db_path=diagnostic_db,
        incoming_dir=tmp_path / "incoming",
        hours=24,
    )

    text = format_runtime_audit(report)

    assert report["source_duckdb_path"] == str(source_db)
    assert report["diagnostic_duckdb_path"] == str(diagnostic_db)
    assert f"source_db     : {source_db}" in text
    assert f"diagnostic_db : {diagnostic_db}" in text
    assert f"incoming_dir  : {tmp_path / 'incoming'}" in text


def test_runtime_audit_formats_no_diagnostic_snapshot_as_none(tmp_path: Path) -> None:
    db = tmp_path / "live.duckdb"
    report = runtime_audit(duckdb_path=db, incoming_dir=None, hours=24)

    text = format_runtime_audit(report)

    assert report["source_duckdb_path"] == str(db)
    assert report["diagnostic_duckdb_path"] is None
    assert f"source_db     : {db}" in text
    assert "diagnostic_db : none" in text


def test_runtime_audit_groups_pending_incoming_by_source_without_db_writes(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    nas = incoming / "nas-claude"
    mac = incoming / "macmini"
    (nas / ".processed").mkdir(parents=True)
    mac.mkdir(parents=True)
    old = nas / "openclaw-old.jsonl"
    newer = nas / "openclaw-new.jsonl"
    mac_file = mac / "batch.jsonl"
    processed = nas / ".processed" / "done.jsonl"
    for path in (old, newer, mac_file, processed):
        path.write_text("{}\n")
    (nas / "inflight.jsonl.tmp").write_text("{}\n")
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    old_ts = now.timestamp() - 3 * 3600
    newer_ts = now.timestamp() - 600
    mac_ts = now.timestamp() - 1200
    os.utime(old, (old_ts, old_ts))
    os.utime(newer, (newer_ts, newer_ts))
    os.utime(mac_file, (mac_ts, mac_ts))
    os.utime(processed, (old_ts, old_ts))

    report = runtime_audit(
        duckdb_path=tmp_path / "missing.duckdb",
        incoming_dir=incoming,
        hours=24,
        now=now,
    )

    assert report["unprocessed_incoming"] == [
        "macmini/batch.jsonl",
        "nas-claude/openclaw-new.jsonl",
        "nas-claude/openclaw-old.jsonl",
    ]
    assert report["pending_incoming_by_source"] == {
        "macmini": {
            "count": 1,
            "oldest_age_seconds": 1200,
            "oldest_age_human": "20m",
            "oldest_file": "macmini/batch.jsonl",
        },
        "nas-claude": {
            "count": 2,
            "oldest_age_seconds": 10800,
            "oldest_age_human": "3h",
            "oldest_file": "nas-claude/openclaw-old.jsonl",
        },
    }
    rendered = format_runtime_audit(report)
    assert "pending incoming jsonl by source:" in rendered
    assert "nas-claude" in rendered
    assert "count=2" in rendered
    assert "oldest_age=3h" in rendered
    assert "destination watcher bottleneck" in rendered
    assert "Mac watcher bottleneck" not in rendered


def test_runtime_audit_separates_stale_running_span_jobs_and_bounds_coverage(
    tmp_path: Path,
) -> None:
    db = tmp_path / "drover.duckdb"
    parquet_dir = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)
    now = datetime.now(timezone.utc)
    _write_span(
        parquet_dir,
        trace_id="trace-current",
        span_id="span-current",
        parent_span_id=None,
        name="llm_call",
        service_name="agentweave",
        start_time=now,
        end_time=now + timedelta(seconds=1),
        duration_ms=1000.0,
        session_id="session-current",
        task_id="task-current",
        agent_id="agent-a",
        cost_usd=0.01,
        attributes_json="{}",
        dedup_key="dedup-current",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "INSERT INTO span_embed_jobs (span_id, status, attempts, updated_at) VALUES ('span-current', 'done', 1, now())"
        )
        con.execute(
            "INSERT INTO span_embed_jobs (span_id, status, attempts, updated_at) VALUES ('span-stale', 'running', 1, now() - INTERVAL '3 days')"
        )
        con.execute(
            "INSERT INTO span_embed_jobs (span_id, status, attempts, updated_at) VALUES ('span-running-fresh', 'running', 1, now())"
        )
        con.execute("""INSERT INTO span_embeddings
               (span_id, trace_id, session_id, task_id, agent_id, source_text,
                source_fields, embedding, model, dim, embedded_at)
               VALUES ('span-current', 'trace-current', 'session-current', 'task-current', 'agent-a', 'prompt: ok',
                       ['prompt_preview'], [0.1, 0.2], 'm', 2, now()),
                      ('span-derived-old', 'trace-old', 'session-old', 'task-old', 'agent-a', 'prompt: old',
                       ['prompt_preview'], [0.1, 0.2], 'm', 2, now())""")
    finally:
        con.close()

    report = runtime_audit(duckdb_path=db, incoming_dir=tmp_path / "incoming", hours=24)

    span_jobs = report["span_embed_jobs"]
    assert span_jobs["status_counts"] == {"done": 1, "running": 2}
    assert span_jobs["running_jobs"] == 2
    assert span_jobs["stale_running_jobs"] == 1
    assert span_jobs["stale_running_age_hours"] >= 72
    assert span_jobs["stale_running"][0]["span_id"] == "span-stale"
    coverage = report["span_embedding_coverage"]
    assert coverage["embedded_spans"] == 2
    assert coverage["embedded_recent_spans"] == 1
    assert coverage["total_recent_spans"] == 1
    assert coverage["pending_jobs"] == 0
    assert coverage["stale_running_jobs"] == 1
    assert coverage["coverage_percent"] == 100.0
    assert "derived or historical" in coverage["coverage_note"]

    formatted = format_runtime_audit(report)
    assert "span_embed_jobs: done=1, running=2 (stale_running=1" in formatted
    assert "coverage=100.0%" in formatted
    assert "2 total embedded; 1 in recent span denominator" in formatted


def test_runtime_audit_reports_summarizer_retryability_without_raw_error_dump(
    tmp_path: Path,
) -> None:
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.executemany(
            "INSERT INTO summarize_jobs (session_id, status, attempts, last_error, updated_at) VALUES (?, 'errored', ?, ?, now())",
            [
                (
                    "runtime-failure",
                    2,
                    "GPU WoL relay unreachable: No route to host; token=secret-value",
                ),
                ("auth-failure", 1, "Error code: 401 - invalid authentication"),
                ("schema-failure", 1, "LLM response missing required keys"),
            ],
        )
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts, last_error, updated_at) VALUES ('pending-job', 'pending', 0, NULL, now())"
        )
    finally:
        con.close()

    report = runtime_audit(duckdb_path=db, incoming_dir=None, hours=24)

    health = report["summarize_jobs"]["backend_health"]
    assert health == {
        "state": "backlog_with_retryable_errors",
        "pending": 1,
        "running": 0,
        "errored": 3,
        "retryable_errors": 2,
        "non_retryable_errors": 1,
        "error_categories": {"auth": 1, "runtime": 1, "validation": 1},
    }
    errors_by_session = {
        row["session_id"]: row for row in report["summarize_jobs"]["recent_errors"]
    }
    assert errors_by_session["schema-failure"]["retryable"] is False
    assert errors_by_session["schema-failure"]["error_category"] == "validation"
    rendered = format_runtime_audit(report)
    assert "summarizer health: backlog_with_retryable_errors" in rendered
    assert "retryable=2 non_retryable=1" in rendered
    assert "category=runtime retryable=yes" in rendered
    assert "secret-value" not in rendered


def test_runtime_audit_reports_summarizer_idle_when_no_pending_or_errors(
    tmp_path: Path,
) -> None:
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts, last_error, updated_at) VALUES ('done-job', 'done', 1, NULL, now())"
        )
    finally:
        con.close()

    report = runtime_audit(duckdb_path=db, incoming_dir=None, hours=24)

    assert report["summarize_jobs"]["backend_health"]["state"] == "idle"
    assert "summarizer health: idle" in format_runtime_audit(report)


def test_runtime_audit_reports_span_metadata_completeness_by_service(
    tmp_path: Path,
) -> None:
    parquet_dir = tmp_path / "parquet"
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)
    now = datetime.now(timezone.utc)

    _write_span(
        parquet_dir,
        trace_id="trace-aw",
        span_id="span-aw",
        parent_span_id=None,
        name="agent.turn",
        service_name="agentweave-proxy",
        start_time=now,
        end_time=now + timedelta(seconds=1),
        duration_ms=1000.0,
        session_id="openclaw-session",
        task_id="task-1",
        agent_id="nas-openclaw",
        cost_usd=0.01,
        attributes_json=(
            '{"prov.harness":"openclaw","prov.project":"OpenClaw",'
            '"prov.repo.owner":"arniesaha","prov.repo.name":"openclaw"}'
        ),
        dedup_key="span-aw",
    )
    _write_span(
        parquet_dir,
        trace_id="trace-linked-openclaw",
        span_id="span-linked-openclaw",
        parent_span_id=None,
        name="agent.turn",
        service_name="agentweave-openclaw",
        start_time=now,
        end_time=now + timedelta(seconds=1),
        duration_ms=1000.0,
        session_id="native-openclaw-session",
        task_id="task-2",
        agent_id="nas-openclaw",
        cost_usd=0.02,
        attributes_json=(
            '{"prov.harness":"openclaw","prov.session.key":"route-key-1",'
            '"prov.activity.type":"agent_turn"}'
        ),
        dedup_key="span-linked-openclaw",
    )
    _write_span(
        parquet_dir,
        trace_id="trace-mixed-openclaw-unlinked",
        span_id="span-mixed-openclaw-unlinked",
        parent_span_id=None,
        name="agent.turn",
        service_name="agentweave-openclaw",
        start_time=now,
        end_time=now + timedelta(seconds=1),
        duration_ms=1000.0,
        session_id="span-only-openclaw-session",
        task_id="task-2b",
        agent_id="nas-openclaw",
        cost_usd=0.02,
        attributes_json=(
            '{"prov.harness":"openclaw","prov.activity.type":"agent_turn"}'
        ),
        dedup_key="span-mixed-openclaw-unlinked",
    )
    _write_span(
        parquet_dir,
        trace_id="trace-needs-attribution",
        span_id="span-needs-attribution",
        parent_span_id=None,
        name="agent.turn",
        service_name="agentweave-unlinked",
        start_time=now,
        end_time=now + timedelta(seconds=1),
        duration_ms=1000.0,
        session_id="span-only-session",
        task_id="task-3",
        agent_id="nas-openclaw",
        cost_usd=0.02,
        attributes_json=(
            '{"prov.harness":"openclaw","prov.activity.type":"agent_turn"}'
        ),
        dedup_key="span-needs-attribution",
    )
    _write_span(
        parquet_dir,
        trace_id="trace-mux",
        span_id="span-mux",
        parent_span_id=None,
        name="mux.route",
        service_name="mux-router",
        start_time=now,
        end_time=now + timedelta(seconds=1),
        duration_ms=1000.0,
        session_id=None,
        task_id=None,
        agent_id="mux-router",
        cost_usd=0.0,
        attributes_json='{"prov.activity.type":"llm_call"}',
        dedup_key="span-mux",
    )

    agent_schema = pa.schema(
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
    agent_out = (
        parquet_dir
        / "agent_events"
        / f"date={now.date().isoformat()}"
        / "agent_id=nas-openclaw"
    )
    agent_out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": pa.array(["native-linked"], type=pa.string()),
                "session_id": pa.array(["native-openclaw-session"], type=pa.string()),
                "agent_id": pa.array(["nas-openclaw"], type=pa.string()),
                "task_id": pa.array(["task-2"], type=pa.string()),
                "timestamp": pa.array([now], type=pa.timestamp("us", tz="UTC")),
                "event_type": pa.array(["assistant_turn"], type=pa.string()),
                "role": pa.array(["assistant"], type=pa.string()),
                "content": pa.array(["linked native event"], type=pa.string()),
                "repo_owner": pa.array(["arniesaha"], type=pa.string()),
                "repo_name": pa.array(["openclaw"], type=pa.string()),
                "branch": pa.array(["main"], type=pa.string()),
                "principal_id": pa.array([None], type=pa.string()),
                "dedup_key": pa.array(["native-linked"], type=pa.string()),
                "raw_data": pa.array(
                    ['{"harness":"openclaw","session_key":"route-key-1"}'],
                    type=pa.string(),
                ),
            },
            schema=agent_schema,
        ),
        agent_out / "part-linked.parquet",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)

    report = runtime_audit(duckdb_path=db, incoming_dir=tmp_path / "incoming", hours=24)
    services = {
        row["service_name"]: row
        for row in report["span_metadata_completeness"]["services"]
    }

    assert services["agentweave-proxy"]["total"] == 1
    assert services["agentweave-proxy"]["missing_repo"] == 0
    assert services["agentweave-proxy"]["missing_project"] == 0
    assert services["agentweave-proxy"]["classification"] == "attributed"
    assert services["agentweave-openclaw"]["total"] == 2
    assert services["agentweave-openclaw"]["missing_repo"] == 2
    assert services["agentweave-openclaw"]["missing_project"] == 2
    assert services["agentweave-openclaw"]["linked_openclaw_spans"] == 1
    assert services["agentweave-openclaw"]["classification"] == "needs_attribution"
    assert services["agentweave-unlinked"]["total"] == 1
    assert services["agentweave-unlinked"]["linked_openclaw_spans"] == 0
    assert services["agentweave-unlinked"]["classification"] == "needs_attribution"
    assert services["mux-router"]["total"] == 1
    assert services["mux-router"]["missing_session_id"] == 1
    assert services["mux-router"]["missing_repo"] == 1
    assert services["mux-router"]["classification"] == "provenance_only"
    rendered = format_runtime_audit(report)
    assert "span metadata completeness by service" in rendered
    assert "linked_openclaw=1" in rendered


def test_span_metadata_derives_project_from_explicit_repo_attrs_for_agentweave_and_mux(
    tmp_path: Path,
) -> None:
    parquet_dir = tmp_path / "parquet"
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)
    now = datetime.now(timezone.utc)

    _write_span(
        parquet_dir,
        trace_id="trace-agentweave-repo-only",
        span_id="span-agentweave-repo-only",
        parent_span_id=None,
        name="agent.turn",
        service_name="agentweave-proxy",
        start_time=now,
        end_time=now + timedelta(seconds=1),
        duration_ms=1000.0,
        session_id=None,
        task_id=None,
        agent_id=None,
        cost_usd=0.01,
        attributes_json=(
            '{"session.id":"sess-agentweave-repo-only",'
            '"prov.repo.owner":"arniesaha",'
            '"prov.repo.name":"nexus",'
            '"prov.agent.id":"claude-code-nas"}'
        ),
        dedup_key="span-agentweave-repo-only",
    )
    _write_span(
        parquet_dir,
        trace_id="trace-mux-attributed",
        span_id="span-mux-attributed",
        parent_span_id=None,
        name="mux.route",
        service_name="mux-router",
        start_time=now,
        end_time=now + timedelta(seconds=1),
        duration_ms=1000.0,
        session_id=None,
        task_id=None,
        agent_id="mux-router",
        cost_usd=0.0,
        attributes_json=(
            '{"prov.session.id":"sess-mux-attributed",'
            '"prov.repo.owner":"arniesaha",'
            '"prov.repo.name":"mux",'
            '"prov.routing.provider":"anthropic",'
            '"prov.routing.model":"claude-sonnet"}'
        ),
        dedup_key="span-mux-attributed",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)

    con = duckdb.connect(str(db))
    try:
        span_rows = con.execute("""
            SELECT service_name, session_id, repo_owner, repo_name, project
            FROM spans
            ORDER BY service_name
            """).fetchall()
    finally:
        con.close()

    assert span_rows == [
        (
            "agentweave-proxy",
            "sess-agentweave-repo-only",
            "arniesaha",
            "nexus",
            "nexus",
        ),
        ("mux-router", "sess-mux-attributed", "arniesaha", "mux", "mux"),
    ]

    report = runtime_audit(duckdb_path=db, incoming_dir=tmp_path / "incoming", hours=24)
    services = {
        (row["service_name"], row["harness"]): row
        for row in report["span_metadata_completeness"]["services"]
    }
    assert services[("agentweave-proxy", "<unknown>")]["classification"] == "attributed"
    assert services[("agentweave-proxy", "<unknown>")]["missing_project"] == 0
    assert services[("mux-router", "<unknown>")]["classification"] == "attributed"
    assert services[("mux-router", "<unknown>")]["missing_project"] == 0


def test_runtime_audit_reports_openclaw_agentweave_contract_health(
    tmp_path: Path,
) -> None:
    parquet_dir = tmp_path / "parquet"
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    agent_schema = pa.schema(
        [
            ("id", pa.string()),
            ("session_id", pa.string()),
            ("timestamp", pa.timestamp("us", tz="UTC")),
            ("event_type", pa.string()),
            ("role", pa.string()),
            ("content", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("task_id", pa.string()),
            ("principal_id", pa.string()),
            ("dedup_key", pa.string()),
            ("raw_data", pa.string()),
        ]
    )
    agent_out = parquet_dir / "agent_events" / f"date={today}" / "agent_id=nas-openclaw"
    agent_out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": ["e1", "e2"],
                "session_id": ["native-session", "unknown_openclaw"],
                "timestamp": [now - timedelta(minutes=5), now - timedelta(minutes=4)],
                "event_type": ["tool_call", "message"],
                "role": [None, "assistant"],
                "content": ["", "ok"],
                "repo_owner": ["arniesaha", "arniesaha"],
                "repo_name": ["openclaw", "openclaw"],
                "branch": ["main", "main"],
                "task_id": ["task", "task"],
                "principal_id": [None, None],
                "dedup_key": ["e1", "e2"],
                "raw_data": [
                    '{"harness":"openclaw","session_key":"agent:main:main","session_uuid":"native-session"}',
                    '{"type":"message","cwd":"/tmp/openclaw"}',
                ],
            },
            schema=agent_schema,
        ),
        agent_out / "part.parquet",
    )

    span_schema = pa.schema(
        [
            ("trace_id", pa.string()),
            ("span_id", pa.string()),
            ("parent_span_id", pa.string()),
            ("name", pa.string()),
            ("service_name", pa.string()),
            ("start_time", pa.timestamp("us", tz="UTC")),
            ("end_time", pa.timestamp("us", tz="UTC")),
            ("duration_ms", pa.float64()),
            ("harness", pa.string()),
            ("session_id", pa.string()),
            ("session_key", pa.string()),
            ("agent_id", pa.string()),
            ("project", pa.string()),
            ("repository", pa.string()),
            ("cwd", pa.string()),
            ("response_preview", pa.string()),
            ("preview_truncated", pa.bool_()),
            ("preview_bytes", pa.int64()),
            ("attributes_json", pa.string()),
            ("cost_usd", pa.float64()),
            ("dedup_key", pa.string()),
        ]
    )
    span_out = parquet_dir / "spans" / f"date={today}"
    span_out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "trace_id": ["trace-1", "trace-2"],
                "span_id": ["span-linked", "span-attr-only"],
                "parent_span_id": [None, None],
                "name": ["openclaw.turn", "openclaw.tool"],
                "service_name": ["agentweave", "agentweave"],
                "start_time": [now - timedelta(minutes=3), now - timedelta(minutes=2)],
                "end_time": [now - timedelta(minutes=2), now - timedelta(minutes=1)],
                "duration_ms": [1.0, 1.0],
                "harness": ["openclaw", None],
                "session_id": ["native-session", "span-only-session"],
                "session_key": ["agent:main:main", None],
                "agent_id": ["nas-openclaw", "nas-openclaw"],
                "project": ["nix", None],
                "repository": ["arniesaha/openclaw", None],
                "cwd": ["/tmp/openclaw", None],
                "response_preview": ["ok", None],
                "preview_truncated": [False, False],
                "preview_bytes": [2000, 2000],
                "attributes_json": [
                    '{"prov.harness":"openclaw","prov.session.key":"agent:main:main"}',
                    '{"prov.harness":"openclaw","prov.session.key":"agent:main:main","prov.project":"nix"}',
                ],
                "cost_usd": [0.0, 0.0],
                "dedup_key": ["s1", "s2"],
            },
            schema=span_schema,
        ),
        span_out / "part.parquet",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)

    report = runtime_audit(duckdb_path=db, incoming_dir=tmp_path / "incoming", hours=24)
    health = report["openclaw_agentweave_health"]

    assert health["native_events"]["total"] == 2
    assert health["native_events"]["raw_harness_openclaw"] == 1
    assert health["native_events"]["raw_session_key_present"] == 1
    assert health["native_events"]["unknown_openclaw_session_rows"] == 1
    assert health["spans"]["column_harness_openclaw"] == 2
    assert health["spans"]["attr_harness_openclaw"] == 2
    assert health["spans"]["attr_openclaw_but_column_harness_null"] == 0
    assert health["spans"]["attr_session_key_but_column_null"] == 0
    assert health["linkability"]["exact_session_id_matches"] == 1
    assert health["linkability"]["openclaw_like_spans"] == 2
    assert "OpenClaw/AgentWeave contract health" in format_runtime_audit(report)


def test_runtime_audit_surfaces_historical_unknown_openclaw_debt(
    tmp_path: Path,
) -> None:
    parquet_dir = tmp_path / "parquet"
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)
    old = datetime.now(timezone.utc) - timedelta(days=3)

    agent_schema = pa.schema(
        [
            ("id", pa.string()),
            ("session_id", pa.string()),
            ("timestamp", pa.timestamp("us", tz="UTC")),
            ("event_type", pa.string()),
            ("role", pa.string()),
            ("content", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("task_id", pa.string()),
            ("principal_id", pa.string()),
            ("dedup_key", pa.string()),
            ("raw_data", pa.string()),
        ]
    )
    agent_out = (
        parquet_dir
        / "agent_events"
        / f"date={old.date().isoformat()}"
        / "agent_id=nas-openclaw"
    )
    agent_out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": ["old-unknown"],
                "session_id": ["unknown_openclaw"],
                "timestamp": [old],
                "event_type": ["tool_call"],
                "role": [None],
                "content": ["legacy placeholder"],
                "repo_owner": ["arniesaha"],
                "repo_name": ["openclaw"],
                "branch": ["main"],
                "task_id": ["task"],
                "principal_id": [None],
                "dedup_key": ["old-unknown"],
                "raw_data": ['{"type":"message"}'],
            },
            schema=agent_schema,
        ),
        agent_out / "part.parquet",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)

    report = runtime_audit(duckdb_path=db, incoming_dir=tmp_path / "incoming", hours=24)
    native = report["openclaw_agentweave_health"]["native_events"]

    assert native["unknown_openclaw_session_rows"] == 0
    assert native["historical_unknown_openclaw_session_rows"] == 1
    assert report["openclaw_agentweave_health"]["status"] == "ok"
    rendered = format_runtime_audit(report)
    assert "active_unknown_openclaw=0" in rendered
    assert "historical_unknown_openclaw=1" in rendered
    assert "excluded from live contract severity" in rendered


def test_runtime_audit_counts_openclaw_linkability_per_span(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "parquet"
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    agent_schema = pa.schema(
        [
            ("id", pa.string()),
            ("session_id", pa.string()),
            ("timestamp", pa.timestamp("us", tz="UTC")),
            ("event_type", pa.string()),
            ("role", pa.string()),
            ("content", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("task_id", pa.string()),
            ("principal_id", pa.string()),
            ("dedup_key", pa.string()),
            ("raw_data", pa.string()),
        ]
    )
    agent_out = parquet_dir / "agent_events" / f"date={today}" / "agent_id=nas-openclaw"
    agent_out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": ["e-exact", "e-key"],
                "session_id": ["native-exact", "native-key-only"],
                "timestamp": [now - timedelta(minutes=5), now - timedelta(minutes=4)],
                "event_type": ["tool_call", "tool_call"],
                "role": [None, None],
                "content": ["", ""],
                "repo_owner": ["arniesaha", "arniesaha"],
                "repo_name": ["openclaw", "openclaw"],
                "branch": ["main", "main"],
                "task_id": ["task", "task"],
                "principal_id": [None, None],
                "dedup_key": ["e-exact", "e-key"],
                "raw_data": [
                    '{"harness":"openclaw","session_uuid":"native-exact"}',
                    '{"harness":"openclaw","session_key":"key-only"}',
                ],
            },
            schema=agent_schema,
        ),
        agent_out / "part.parquet",
    )

    span_schema = pa.schema(
        [
            ("trace_id", pa.string()),
            ("span_id", pa.string()),
            ("name", pa.string()),
            ("start_time", pa.timestamp("us", tz="UTC")),
            ("end_time", pa.timestamp("us", tz="UTC")),
            ("duration_ms", pa.float64()),
            ("harness", pa.string()),
            ("session_id", pa.string()),
            ("session_key", pa.string()),
            ("agent_id", pa.string()),
            ("attributes_json", pa.string()),
            ("dedup_key", pa.string()),
        ]
    )
    span_out = parquet_dir / "spans" / f"date={today}"
    span_out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "trace_id": ["trace-1", "trace-2", "trace-3"],
                "span_id": ["exact", "key", "miss"],
                "name": ["openclaw.turn", "openclaw.turn", "openclaw.turn"],
                "start_time": [
                    now - timedelta(minutes=3),
                    now - timedelta(minutes=2),
                    now - timedelta(minutes=1),
                ],
                "end_time": [now, now, now],
                "duration_ms": [1.0, 1.0, 1.0],
                "harness": ["openclaw", "openclaw", "openclaw"],
                "session_id": ["native-exact", "span-key-only", "span-missing"],
                "session_key": [None, "key-only", "missing-key"],
                "agent_id": ["nas-openclaw", "nas-openclaw", "nas-openclaw"],
                "attributes_json": ["{}", "{}", "{}"],
                "dedup_key": ["s-exact", "s-key", "s-miss"],
            },
            schema=span_schema,
        ),
        span_out / "part.parquet",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)

    report = runtime_audit(duckdb_path=db, incoming_dir=tmp_path / "incoming", hours=24)
    linkability = report["openclaw_agentweave_health"]["linkability"]

    assert linkability["openclaw_like_spans"] == 3
    assert linkability["exact_session_id_matches"] == 1
    assert linkability["session_key_matches"] == 1
    assert linkability["matched_spans"] == 2
    assert linkability["unmatched_spans"] == 1
    assert report["openclaw_agentweave_health"]["status"] == "warn"


def test_runtime_audit_counts_all_native_openclaw_session_keys(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "parquet"
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    agent_schema = pa.schema(
        [
            ("id", pa.string()),
            ("session_id", pa.string()),
            ("timestamp", pa.timestamp("us", tz="UTC")),
            ("event_type", pa.string()),
            ("role", pa.string()),
            ("content", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("task_id", pa.string()),
            ("principal_id", pa.string()),
            ("dedup_key", pa.string()),
            ("raw_data", pa.string()),
        ]
    )
    agent_out = parquet_dir / "agent_events" / f"date={today}" / "agent_id=nas-openclaw"
    agent_out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": ["e-key-a", "e-key-b"],
                "session_id": ["unknown_openclaw", "unknown_openclaw"],
                "timestamp": [now - timedelta(minutes=5), now - timedelta(minutes=4)],
                "event_type": ["tool_call", "tool_call"],
                "role": [None, None],
                "content": ["", ""],
                "repo_owner": ["arniesaha", "arniesaha"],
                "repo_name": ["openclaw", "openclaw"],
                "branch": ["main", "main"],
                "task_id": ["task", "task"],
                "principal_id": [None, None],
                "dedup_key": ["e-key-a", "e-key-b"],
                "raw_data": [
                    '{"harness":"openclaw","session_key":"key-a"}',
                    '{"harness":"openclaw","session_key":"key-b"}',
                ],
            },
            schema=agent_schema,
        ),
        agent_out / "part.parquet",
    )

    span_schema = pa.schema(
        [
            ("trace_id", pa.string()),
            ("span_id", pa.string()),
            ("name", pa.string()),
            ("start_time", pa.timestamp("us", tz="UTC")),
            ("end_time", pa.timestamp("us", tz="UTC")),
            ("duration_ms", pa.float64()),
            ("harness", pa.string()),
            ("session_id", pa.string()),
            ("session_key", pa.string()),
            ("agent_id", pa.string()),
            ("attributes_json", pa.string()),
            ("dedup_key", pa.string()),
        ]
    )
    span_out = parquet_dir / "spans" / f"date={today}"
    span_out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "trace_id": ["trace-a", "trace-b"],
                "span_id": ["span-a", "span-b"],
                "name": ["openclaw.turn", "openclaw.turn"],
                "start_time": [now - timedelta(minutes=3), now - timedelta(minutes=2)],
                "end_time": [now, now],
                "duration_ms": [1.0, 1.0],
                "harness": ["openclaw", "openclaw"],
                "session_id": ["span-a", "span-b"],
                "session_key": ["wrong-but-present", None],
                "agent_id": ["nas-openclaw", "nas-openclaw"],
                "attributes_json": [
                    '{"prov.session.key":"key-a"}',
                    '{"prov.session.key":"key-b"}',
                ],
                "dedup_key": ["span-a", "span-b"],
            },
            schema=span_schema,
        ),
        span_out / "part.parquet",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)

    report = runtime_audit(duckdb_path=db, incoming_dir=tmp_path / "incoming", hours=24)
    linkability = report["openclaw_agentweave_health"]["linkability"]

    assert linkability["openclaw_like_spans"] == 2
    assert linkability["session_key_matches"] == 2
    assert linkability["matched_spans"] == 2
    assert linkability["unmatched_spans"] == 0


def test_runtime_audit_does_not_exact_link_unknown_openclaw_placeholder(
    tmp_path: Path,
) -> None:
    parquet_dir = tmp_path / "parquet"
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    agent_schema = pa.schema(
        [
            ("id", pa.string()),
            ("session_id", pa.string()),
            ("timestamp", pa.timestamp("us", tz="UTC")),
            ("event_type", pa.string()),
            ("role", pa.string()),
            ("content", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("task_id", pa.string()),
            ("principal_id", pa.string()),
            ("dedup_key", pa.string()),
            ("raw_data", pa.string()),
        ]
    )
    agent_out = parquet_dir / "agent_events" / f"date={today}" / "agent_id=nas-openclaw"
    agent_out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": ["e-unknown"],
                "session_id": ["unknown_openclaw"],
                "timestamp": [now - timedelta(minutes=5)],
                "event_type": ["tool_call"],
                "role": [None],
                "content": [""],
                "repo_owner": ["arniesaha"],
                "repo_name": ["openclaw"],
                "branch": ["main"],
                "task_id": ["task"],
                "principal_id": [None],
                "dedup_key": ["e-unknown"],
                "raw_data": ['{"harness":"openclaw"}'],
            },
            schema=agent_schema,
        ),
        agent_out / "part.parquet",
    )

    span_schema = pa.schema(
        [
            ("trace_id", pa.string()),
            ("span_id", pa.string()),
            ("name", pa.string()),
            ("start_time", pa.timestamp("us", tz="UTC")),
            ("end_time", pa.timestamp("us", tz="UTC")),
            ("duration_ms", pa.float64()),
            ("harness", pa.string()),
            ("session_id", pa.string()),
            ("session_key", pa.string()),
            ("agent_id", pa.string()),
            ("attributes_json", pa.string()),
            ("dedup_key", pa.string()),
        ]
    )
    span_out = parquet_dir / "spans" / f"date={today}"
    span_out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "trace_id": ["trace-unknown"],
                "span_id": ["span-unknown"],
                "name": ["openclaw.turn"],
                "start_time": [now - timedelta(minutes=2)],
                "end_time": [now],
                "duration_ms": [1.0],
                "harness": ["openclaw"],
                "session_id": ["unknown_openclaw"],
                "session_key": [None],
                "agent_id": ["nas-openclaw"],
                "attributes_json": ["{}"],
                "dedup_key": ["span-unknown"],
            },
            schema=span_schema,
        ),
        span_out / "part.parquet",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)

    report = runtime_audit(duckdb_path=db, incoming_dir=tmp_path / "incoming", hours=24)
    linkability = report["openclaw_agentweave_health"]["linkability"]

    assert linkability["exact_session_id_matches"] == 0
    assert linkability["matched_spans"] == 0
    assert linkability["unmatched_spans"] == 1
    assert report["openclaw_agentweave_health"]["status"] == "warn"


def test_runtime_audit_span_health_is_partition_safe_when_agent_events_fail(
    tmp_path: Path,
) -> None:
    """Span health must not depend on a broad scan of agent_events.

    The production failure behind #69 happened because querying ``spans``
    evaluated broad attribution CTEs over ``agent_events`` parquet and hit the
    process file-descriptor limit. A corrupt old agent_events parquet is a
    deterministic proxy for "do not touch that side of the lake" in this unit
    test: the span freshness query should still succeed because it only needs
    the recent spans partition.
    """

    parquet_dir = tmp_path / "parquet"
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db)
    now = datetime.now(timezone.utc)
    _write_span(
        parquet_dir,
        trace_id="trace-1",
        span_id="span-1",
        parent_span_id=None,
        name="llm_call",
        service_name="claude-code",
        start_time=now - timedelta(minutes=10),
        end_time=now - timedelta(minutes=9),
        duration_ms=60_000.0,
        session_id="aw-session",
        task_id="aw-task",
        agent_id="nas-claude",
        cost_usd=0.42,
        dedup_key="span1",
    )
    bad_agent_events = (
        parquet_dir
        / "agent_events"
        / "date=2026-01-01"
        / "agent_id=old-agent"
        / "part-bad.parquet"
    )
    bad_agent_events.parent.mkdir(parents=True, exist_ok=True)
    bad_agent_events.write_text("not parquet")

    report = runtime_audit(duckdb_path=db, incoming_dir=tmp_path / "missing", hours=24)
    text = format_runtime_audit(report)

    assert report["table_counts"]["agent_events"] is None
    assert report["table_counts"]["spans"] == 1
    assert report["span_health"]["recent_count"] == 1
    assert report["span_health"]["latest_start"] is not None
    assert "span health" in text


def test_runtime_audit_reports_agent_event_identity_duplicates(tmp_path: Path) -> None:
    db = tmp_path / "duplicates.duckdb"
    con = duckdb.connect(str(db))
    try:
        con.execute("""
            CREATE TABLE agent_events (
                id VARCHAR,
                session_id VARCHAR,
                agent_id VARCHAR,
                timestamp VARCHAR,
                event_type VARCHAR,
                content VARCHAR,
                repo_owner VARCHAR,
                repo_name VARCHAR,
                dedup_key VARCHAR,
                date VARCHAR
            )
            """)
        con.execute("""
            INSERT INTO agent_events VALUES
              ('evt-tz', 's1', 'agent-a', '2026-03-25 00:26:31.763-07', 'UserPromptSubmit', 'same', 'acme', 'repo', 'dedup-offset', '2026-03-25'),
              ('evt-tz', 's1', 'agent-a', '2026-03-25 07:26:31', 'UserPromptSubmit', 'same', 'acme', 'repo', 'dedup-utc', '2026-03-25'),
              ('evt-unique', 's2', 'agent-a', '2026-03-25 07:27:31', 'Stop', 'done', 'acme', 'repo', 'dedup-b', '2026-03-25')
            """)
        con.execute("CREATE TABLE spans (span_id VARCHAR)")
        con.execute("CREATE TABLE tasks (task_id VARCHAR)")
        con.execute("CREATE TABLE session_summaries (session_id VARCHAR)")
        con.execute(
            "CREATE TABLE summarize_jobs (session_id VARCHAR, status VARCHAR, attempts INTEGER, last_error VARCHAR, updated_at TIMESTAMP, enqueued_at TIMESTAMP)"
        )
        con.execute(
            "CREATE TABLE embed_jobs (session_id VARCHAR, status VARCHAR, attempts INTEGER, last_error VARCHAR, updated_at TIMESTAMP, enqueued_at TIMESTAMP)"
        )
        con.execute(
            "CREATE TABLE session_embeddings (session_id VARCHAR, embedding FLOAT[], model VARCHAR, dim INTEGER, embedded_at TIMESTAMP)"
        )
    finally:
        con.close()

    report = runtime_audit(duckdb_path=db, incoming_dir=tmp_path / "missing", hours=24)

    identity = report["agent_event_identity"]
    assert identity["status"] == "ok"
    assert identity["canonical_semantics"] == "dedup_key"
    assert (
        identity["source_id_context"] == "source/provenance only; not canonical health"
    )
    assert identity["duplicate_id_values"] == 1
    assert identity["duplicate_id_rows"] == 1
    assert identity["duplicate_dedup_key_values"] == 0
    assert identity["duplicate_dedup_key_rows"] == 0
    assert identity["duplicate_id_examples"] == [
        {"id": "evt-tz", "rows": 2, "dedup_keys": 2}
    ]
    assert not any("duplicate id values" in warning for warning in report["warnings"])
    assert not any("duplicate dedup_key" in warning for warning in report["warnings"])


def test_runtime_audit_flags_duplicate_canonical_dedup_keys(tmp_path: Path) -> None:
    db = tmp_path / "duplicates.duckdb"
    con = duckdb.connect(str(db))
    try:
        con.execute("""
            CREATE TABLE agent_events (
                id VARCHAR,
                session_id VARCHAR,
                agent_id VARCHAR,
                timestamp VARCHAR,
                event_type VARCHAR,
                content VARCHAR,
                repo_owner VARCHAR,
                repo_name VARCHAR,
                dedup_key VARCHAR,
                date VARCHAR
            )
            """)
        con.execute("""
            INSERT INTO agent_events VALUES
              ('source-a', 's1', 'agent-a', '2026-03-25 07:26:31', 'UserPromptSubmit', 'same', 'acme', 'repo', 'canonical-dup', '2026-03-25'),
              ('source-b', 's1', 'agent-a', '2026-03-25 07:26:32', 'UserPromptSubmit', 'same', 'acme', 'repo', 'canonical-dup', '2026-03-25')
            """)
        con.execute("CREATE TABLE spans (span_id VARCHAR)")
        con.execute("CREATE TABLE tasks (task_id VARCHAR)")
        con.execute("CREATE TABLE session_summaries (session_id VARCHAR)")
        con.execute("CREATE TABLE summarize_jobs (session_id VARCHAR, status VARCHAR)")
        con.execute("CREATE TABLE embed_jobs (session_id VARCHAR, status VARCHAR)")
        con.execute("CREATE TABLE session_embeddings (session_id VARCHAR)")
    finally:
        con.close()

    report = runtime_audit(duckdb_path=db, incoming_dir=tmp_path / "missing", hours=24)

    identity = report["agent_event_identity"]
    assert identity["status"] == "duplicate_dedup_key"
    assert identity["duplicate_id_values"] == 0
    assert identity["duplicate_dedup_key_values"] == 1
    assert identity["duplicate_dedup_key_rows"] == 1
    assert any(
        "duplicate dedup_key values" in warning for warning in report["warnings"]
    )


def test_runtime_audit_flags_embedding_queue_with_no_vectors(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO embed_jobs (session_id, status, attempts, updated_at) VALUES ('s1', 'pending', 0, now())"
        )
        con.execute(
            "INSERT INTO embed_jobs (session_id, status, attempts, updated_at) VALUES ('s2', 'pending', 0, now())"
        )
    finally:
        con.close()

    report = runtime_audit(duckdb_path=duckdb_path, hours=24)
    text = format_runtime_audit(report)

    assert report["embedding_status"]["state"] == "offline_or_unconfigured"
    assert "2 pending embed jobs" in report["embedding_status"]["message"]
    assert (
        "Embedding queue has 2 pending jobs but 0 session_embeddings"
        in report["warnings"]
    )
    assert "embedding status: offline_or_unconfigured" in text
    assert "start drover-server run without --no-embeddings" in text


class _RecordingConnection:
    """Delegating DuckDB connection proxy that records executed SQL."""

    def __init__(self, inner: duckdb.DuckDBPyConnection) -> None:
        self._inner = inner
        self.statements: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.statements.append(str(sql))
        return self._inner.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _full_history_agent_event_scans(statements: list[str]) -> list[str]:
    """Return statements that scan ``agent_events`` with no partition bound.

    ``date >=`` is the only predicate that lets DuckDB prune the hive
    partitions under ``agent_events/``. A statement naming ``agent_events``
    without one reads the entire historical parquet tree.
    """
    scans = []
    for statement in statements:
        collapsed = " ".join(statement.split()).lower()
        if "agent_events" not in collapsed:
            continue
        if "date >=" in collapsed:
            continue
        scans.append(collapsed)
    return scans


def test_metrics_hot_path_does_not_repeat_full_history_agent_event_scans(
    tmp_path: Path, monkeypatch
) -> None:
    """Guard the /metrics hot path against re-scanning all of agent_events.

    ``quality_snapshot(deep=False)`` drives the Prometheus endpoint every
    refresh. Each unbounded ``agent_events`` statement reads the whole parquet
    tree, so the cost of this audit is set by how many such statements it
    issues, not by how much work each one does. Regression guard for #78.

    Statement-level profile against a 6,876-file / 2.23M-row store at
    ``threads=1``: the three whole-history statements this used to allow --
    the row count, the session-set metrics and the duplicate-identity
    metrics -- cost 1.61s + 2.37s + 2.71s of an 11.4s audit. They read three
    columns between them, so they now share one pass.
    """
    from drover.server import doctor as doctor_module

    db, incoming = _seed_runtime_db(tmp_path)
    recorded: list[_RecordingConnection] = []
    real_open = doctor_module.open_duckdb_connection

    def _recording_open(*args, **kwargs):
        connection = _RecordingConnection(real_open(*args, **kwargs))
        recorded.append(connection)
        return connection

    monkeypatch.setattr(doctor_module, "open_duckdb_connection", _recording_open)

    report = runtime_audit(duckdb_path=db, incoming_dir=incoming, hours=24, deep=False)

    statements = [
        statement for connection in recorded for statement in connection.statements
    ]
    scans = _full_history_agent_event_scans(statements)

    # One shared pass feeding the row count, the session-set metrics and the
    # duplicate-identity metrics.
    assert len(scans) == 1, (
        f"{len(scans)} unbounded agent_events scans in the /metrics hot path:\n"
        + "\n".join(f"  - {scan[:120]}" for scan in scans)
    )
    # The audit must still report what it reported before the consolidation.
    assert report["table_counts"]["agent_events"] == 10
    assert report["session_consistency"]["event_sessions"] == 9
    assert report["agent_event_identity"]["status"] == "ok"


def test_shared_agent_event_pass_matches_reading_agent_events_directly() -> None:
    """The one-pass numbers must equal what three separate scans reported.

    The pass pre-aggregates ``(id, dedup_key, session_id)`` with a row count,
    so every consumer now sums counts instead of counting rows. NULL ids,
    NULL dedup_keys and repeated pairs are exactly where that rewrite could
    drift, so they are all present here.
    """
    con = duckdb.connect(":memory:")
    try:
        con.execute(
            "CREATE TABLE agent_events "
            "(id VARCHAR, dedup_key VARCHAR, session_id VARCHAR)"
        )
        con.execute("""
            INSERT INTO agent_events VALUES
              ('a', 'k1', 's1'),
              ('a', 'k1', 's1'),
              ('a', 'k2', 's2'),
              (NULL, 'k2', 's2'),
              ('b', NULL, NULL),
              (NULL, NULL, 's3')
            """)
        direct = audit_agent_event_identity(con)

        scan = scan_agent_events_once(con)
        assert scan is not None
        pooled = audit_agent_event_identity(con, scan=scan)
    finally:
        con.close()

    assert scan.total_rows == 6
    assert pooled == direct


def test_shared_agent_event_pass_leaves_nothing_in_the_database(
    tmp_path: Path,
) -> None:
    """The pass is connection-local, so the audit stays a read-only reader.

    ``runtime_audit`` documents that it never mutates the database. The shared
    pass is the audit's only DDL, and it must stay a TEMP table: anything
    persisted here would be written into the live store by every CLI run and
    into the /metrics copy on every refresh.
    """
    db, incoming = _seed_runtime_db(tmp_path)

    runtime_audit(duckdb_path=db, incoming_dir=incoming, hours=24, deep=False)

    con = duckdb.connect(str(db))
    try:
        persisted = con.execute("SELECT table_name FROM duckdb_tables()").fetchall()
    finally:
        con.close()
    assert "agent_event_scan" not in {str(name) for (name,) in persisted}


def test_scan_agent_events_once_reports_missing_relation() -> None:
    """No ``agent_events`` relation means no pass, and callers fall back."""
    con = duckdb.connect(":memory:")
    try:
        assert scan_agent_events_once(con) is None
    finally:
        con.close()


def test_agent_event_identity_without_dedup_key_column() -> None:
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE agent_events (id VARCHAR)")
        con.execute("INSERT INTO agent_events VALUES ('dup'), ('dup'), ('solo')")

        report = audit_agent_event_identity(con)
    finally:
        con.close()

    assert report["status"] == "missing_dedup_key_column"
    assert report["duplicate_id_values"] == 1
    assert report["duplicate_id_rows"] == 1
    assert report["duplicate_dedup_key_values"] == 0
    assert report["duplicate_id_examples"] == [
        {"id": "dup", "rows": 2, "dedup_keys": 0}
    ]


def test_agent_event_identity_without_id_column() -> None:
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE agent_events (dedup_key VARCHAR)")
        con.execute("INSERT INTO agent_events VALUES ('k'), ('k'), ('other')")

        report = audit_agent_event_identity(con)
    finally:
        con.close()

    assert report["status"] == "duplicate_dedup_key"
    assert report["duplicate_dedup_key_values"] == 1
    assert report["duplicate_dedup_key_rows"] == 1
    assert report["duplicate_id_values"] == 0
    assert report["duplicate_id_examples"] == []


def test_agent_event_identity_reports_missing_relation() -> None:
    con = duckdb.connect(":memory:")
    try:
        report = audit_agent_event_identity(con)
    finally:
        con.close()

    assert report["status"] == "missing"
    assert report["duplicate_id_values"] == 0
    assert report["duplicate_dedup_key_values"] == 0


def test_agent_event_identity_counts_rows_with_null_ids() -> None:
    """A NULL ``id`` must not hide its row from the dedup_key rollup."""
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE agent_events (id VARCHAR, dedup_key VARCHAR)")
        con.execute("""
            INSERT INTO agent_events VALUES
              (NULL, 'shared'),
              ('a', 'shared'),
              ('a', 'other'),
              ('a', NULL)
            """)

        report = audit_agent_event_identity(con)
    finally:
        con.close()

    assert report["duplicate_dedup_key_values"] == 1
    assert report["duplicate_dedup_key_rows"] == 1
    assert report["duplicate_id_values"] == 1
    assert report["duplicate_id_rows"] == 2
    assert report["duplicate_id_examples"] == [{"id": "a", "rows": 3, "dedup_keys": 2}]


def test_runtime_audit_handles_missing_tables_and_incoming_dir(tmp_path: Path) -> None:
    db = tmp_path / "minimal.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE summarize_jobs (session_id VARCHAR, status VARCHAR)")
    con.close()

    report = runtime_audit(duckdb_path=db, incoming_dir=tmp_path / "missing", hours=24)

    assert report["table_counts"]["summarize_jobs"] == 0
    assert report["table_counts"]["agent_events"] is None
    assert report["latest_events"] == {}
    assert report["agent_event_identity"]["status"] == "missing"
    assert report["unprocessed_incoming"] == []
    assert report["warnings"] == []


def test_format_runtime_audit_is_concise(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "DROVER_GENERAL_WORKSPACE_ROOTS",
        "/Users/arnabmac:/home/Arnab:/Users/arnabmac/.claude-mem/observer-sessions",
    )
    db, incoming = _seed_runtime_db(tmp_path)
    text = format_runtime_audit(
        runtime_audit(duckdb_path=db, incoming_dir=incoming, hours=24)
    )

    assert "Drover runtime audit" in text
    assert "source_db" in text
    assert "diagnostic_db : none" in text
    assert "incoming_dir" in text
    assert "latest event by agent" in text
    assert "summarize_jobs" in text
    assert "session consistency" in text
    assert "missing_summaries=9" in text
    assert "agent_event identity" in text
    assert "duplicate_id_values=0" in text
    assert "repo attribution (last 24h)" in text
    assert "repo attribution (last 7d)" in text
    assert "top unattributed cwd/workspace samples (last 24h)" in text
    assert "general workspace cwd samples (last 24h)" in text
    assert "Claude attribution gap categories (last 24h)" in text
    assert "missing producer metadata" in text
    assert "general-context activity" in text
    assert "/Users/arnabmac" in text
    assert "/home/Arnab" in text
    assert "agent-a/stuck.jsonl" in text


def test_cli_runtime_audit_accepts_db_and_incoming_dir(tmp_path: Path) -> None:
    db, incoming = _seed_runtime_db(tmp_path)
    res = CliRunner().invoke(
        main, ["runtime-audit", "--db", str(db), "--incoming-dir", str(incoming)]
    )

    assert res.exit_code == 0, res.output
    assert "Drover runtime audit" in res.output
    assert f"source_db     : {db}" in res.output
    assert "diagnostic_db :" in res.output
    assert "diagnostic_db : none" not in res.output
    assert "agent-a/stuck.jsonl" in res.output


def _record_open_roles(monkeypatch) -> list[str]:
    from drover.server import doctor as doctor_module

    roles: list[str] = []
    real_open = doctor_module.open_duckdb_connection

    def _recording_open(*args, **kwargs):
        roles.append(str(kwargs.get("role", "worker")))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(doctor_module, "open_duckdb_connection", _recording_open)
    return roles


def test_runtime_audit_defaults_to_the_single_threaded_diagnostic_role(
    tmp_path: Path, monkeypatch
) -> None:
    """Callers that may be pointed at the live database keep threads=1.

    ``runtime_audit`` is reachable from the CLI and from in-process
    diagnostics that read the live file. ``threads`` is a DuckDB instance
    setting, so raising it there raises it for every other connection to the
    live instance -- the 2026-08-04 outage (#91).
    """
    db, incoming = _seed_runtime_db(tmp_path)
    roles = _record_open_roles(monkeypatch)

    runtime_audit(duckdb_path=db, incoming_dir=incoming, hours=24, deep=False)

    assert roles == ["diagnostic"]


def test_runtime_audit_can_run_under_the_isolated_copy_role(
    tmp_path: Path, monkeypatch
) -> None:
    """Callers reading a private copy may ask for the parallel role.

    Regression guard for #78: without this the /metrics snapshot stays pinned
    to one thread even though it owns its own DuckDB instance.
    """
    db, incoming = _seed_runtime_db(tmp_path)
    roles = _record_open_roles(monkeypatch)

    report = runtime_audit(
        duckdb_path=db, incoming_dir=incoming, hours=24, deep=False, role="snapshot"
    )

    assert roles == ["snapshot"]
    # Same answers, just more threads.
    assert report["table_counts"]["agent_events"] == 10
    assert report["session_consistency"]["event_sessions"] == 9
