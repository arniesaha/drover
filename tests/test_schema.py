"""Tests for src/drover/schema.py."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from drover.event_identity import canonical_agent_events_cte
from drover.schema import bootstrap, EXPECTED_TABLES, EXPECTED_VIEWS


@pytest.fixture
def tmp_lakehouse(tmp_path):
    parquet_dir = tmp_path / "parquet"
    db_path = tmp_path / "drover.duckdb"
    return parquet_dir, db_path


def test_bootstrap_creates_parquet_dirs(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    for sub in [
        "agent_events",
        "spans",
        "pr_events",
        "routing",
        "provider_usage_snapshots",
    ]:
        assert (parquet_dir / sub).is_dir(), f"missing {sub} dir"


def test_bootstrap_creates_provider_storage(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)

    assert (parquet_dir / "provider_usage_snapshots").is_dir()
    con = duckdb.connect(str(db_path))
    try:
        assert con.execute(
            "SELECT count(*) FROM provider_usage_snapshots"
        ).fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM provider_connections").fetchone() == (
            0,
        )
    finally:
        con.close()


def test_bootstrap_creates_advisory_storage(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)

    con = duckdb.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_type = 'BASE TABLE'"
            ).fetchall()
        }
        assert {"advisory_findings", "advisory_occurrences"} <= tables

        finding_columns = {
            row[0]
            for row in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'advisory_findings'"
            ).fetchall()
        }
        assert {
            "finding_id",
            "fingerprint",
            "analyzer_id",
            "rule_id",
            "target_type",
            "target_id",
            "analyzer_class",
            "severity",
            "confidence",
            "title",
            "impact",
            "remediation_json",
            "state",
            "dismissal_reason",
            "first_seen_at",
            "last_seen_at",
            "resolved_at",
            "dismissed_at",
            "regressed_at",
            "evaluated_content_hash",
            "latest_run_id",
        } <= finding_columns
    finally:
        con.close()


def test_bootstrap_creates_expected_tables(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    tables = {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE'"
        ).fetchall()
    }
    for t in EXPECTED_TABLES:
        assert t in tables, f"missing table {t}"


def test_bootstrap_is_idempotent(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    # Second call must not raise
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)


def test_tasks_table_has_expected_columns(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    cols = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tasks'"
        ).fetchall()
    }
    expected = {
        "task_id",
        "repo_owner",
        "repo_name",
        "branch",
        "explicit_task_id",
        "principal_id",
        "status",
        "title",
        "created_at",
        "last_activity_at",
        "session_count",
        "total_cost_usd",
    }
    assert expected.issubset(cols), f"tasks missing: {expected - cols}"


def test_session_summaries_table_has_expected_columns(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    cols = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'session_summaries'"
        ).fetchall()
    }
    expected = {
        "session_id",
        "task_id",
        "agent_id",
        "ended_at",
        "summary_md",
        "files_touched",
        "tools_used",
        "last_user_prompt",
        "last_assistant",
        "next_steps_md",
        "open_questions",
        "status",
        "generator_model",
        "generated_at",
    }
    assert expected.issubset(cols), f"session_summaries missing: {expected - cols}"


def test_sessions_view_counts_canonical_logical_events(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)

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
    now = datetime(2026, 5, 22, 12, tzinfo=timezone.utc)
    rows = [
        {
            "id": "evt-source",
            "session_id": "sess-identity",
            "agent_id": "agent-a",
            "task_id": "task-a",
            "timestamp": now,
            "event_type": "user_message",
            "role": "user",
            "content": "same logical event",
            "repo_owner": "arniesaha",
            "repo_name": "nexus",
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "logical-a",
            "raw_data": "{}",
        },
        {
            "id": "evt-source",
            "session_id": "sess-identity",
            "agent_id": "agent-a",
            "task_id": "task-a",
            "timestamp": now + timedelta(seconds=1),
            "event_type": "user_message",
            "role": "user",
            "content": "same logical event normalized",
            "repo_owner": None,
            "repo_name": None,
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "logical-a",
            "raw_data": "{}",
        },
        {
            "id": "evt-source",
            "session_id": "sess-identity",
            "agent_id": "agent-a",
            "task_id": "task-a",
            "timestamp": now + timedelta(seconds=2),
            "event_type": "assistant_message",
            "role": "assistant",
            "content": "distinct logical event sharing source id",
            "repo_owner": "arniesaha",
            "repo_name": "nexus",
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "logical-b",
            "raw_data": "{}",
        },
    ]
    out = parquet_dir / "agent_events" / "date=2026-05-22" / "agent_id=agent-a"
    out.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            field.name: pa.array(
                [row.get(field.name) for row in rows],
                type=field.type,
            )
            for field in schema
        },
        schema=schema,
    )
    pq.write_table(table, out / "part-identity.parquet")
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)

    con = duckdb.connect(str(db_path))
    try:
        event_count = con.execute(
            "SELECT event_count FROM sessions WHERE session_id = 'sess-identity'"
        ).fetchone()[0]
        canonical_row = con.execute(f"""
            WITH {canonical_agent_events_cte()}
            SELECT repo_owner, repo_name, content
            FROM canonical_agent_events
            WHERE dedup_key = 'logical-a'
            """).fetchone()
    finally:
        con.close()

    assert event_count == 2
    assert canonical_row == ("arniesaha", "nexus", "same logical event")


def test_agent_events_view_attributes_paperclip_nexus_cwd(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)

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
    now = datetime(2026, 6, 19, 20, tzinfo=timezone.utc)
    out = (
        parquet_dir
        / "agent_events"
        / f"date={now.date().isoformat()}"
        / "agent_id=macmini-claude"
    )
    out.mkdir(parents=True, exist_ok=True)
    row = {
        "id": "paperclip-source",
        "session_id": "paperclip-session",
        "agent_id": "macmini-claude",
        "task_id": None,
        "timestamp": now,
        "event_type": "assistant_message",
        "role": "assistant",
        "content": "implemented ledger",
        "repo_owner": None,
        "repo_name": None,
        "branch": None,
        "principal_id": "arnab",
        "dedup_key": "paperclip-logical",
        "raw_data": json.dumps(
            {
                "cwd": "/paperclip/home/instances/default/workspaces/e46aa686-4fa6-414c-94a7-946538fb308f/nexus/src"
            }
        ),
    }
    cols = {
        field.name: pa.array([row.get(field.name)], type=field.type) for field in schema
    }
    pq.write_table(pa.table(cols, schema=schema), out / "paperclip.parquet")

    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        result = con.execute("""
            SELECT repo_owner, repo_name
            FROM agent_events
            WHERE dedup_key = 'paperclip-logical'
            """).fetchone()
    finally:
        con.close()

    assert result == ("arniesaha", "nexus")


def test_openclaw_span_links_view_reports_non_destructive_link_methods(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)

    now = datetime(2026, 5, 31, 12, tzinfo=timezone.utc)
    event_schema = pa.schema(
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
    event_rows = [
        {
            "id": "evt-canonical",
            "session_id": "session-uuid-1",
            "agent_id": "nas-openclaw",
            "task_id": "task-1",
            "timestamp": now,
            "event_type": "user_turn",
            "role": "user",
            "content": "canonical session event",
            "repo_owner": "example",
            "repo_name": "nexus-demo",
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "evt-canonical",
            "raw_data": json.dumps(
                {"harness": "openclaw", "session_key": "agent:main:main"}
            ),
        },
        {
            "id": "evt-key",
            "session_id": "legacy-session-without-uuid",
            "agent_id": "nas-openclaw",
            "task_id": "task-1",
            "timestamp": now + timedelta(seconds=1),
            "event_type": "assistant_turn",
            "role": "assistant",
            "content": "session key event",
            "repo_owner": "example",
            "repo_name": "nexus-demo",
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "evt-key",
            "raw_data": json.dumps(
                {"harness": "openclaw", "session_key": "agent:key-only:main"}
            ),
        },
        {
            "id": "evt-parent",
            "session_id": "parent-session-uuid",
            "agent_id": "nas-openclaw",
            "task_id": "task-1",
            "timestamp": now + timedelta(seconds=2),
            "event_type": "lifecycle",
            "role": None,
            "content": "parent session event",
            "repo_owner": "example",
            "repo_name": "nexus-demo",
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "evt-parent",
            "raw_data": json.dumps({"harness": "openclaw"}),
        },
    ]
    event_dir = (
        parquet_dir / "agent_events" / "date=2026-05-31" / "agent_id=nas-openclaw"
    )
    event_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                field.name: pa.array(
                    [row.get(field.name) for row in event_rows], type=field.type
                )
                for field in event_schema
            },
            schema=event_schema,
        ),
        event_dir / "part-openclaw.parquet",
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
            ("parent_session_id", pa.string()),
            ("agent_id", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("dedup_key", pa.string()),
        ]
    )
    span_rows = [
        {
            "trace_id": "trace-canonical",
            "span_id": "span-canonical",
            "parent_span_id": None,
            "name": "turn",
            "service_name": "agentweave",
            "start_time": now,
            "end_time": now + timedelta(milliseconds=10),
            "duration_ms": 10.0,
            "harness": "openclaw",
            "session_id": "session-uuid-1",
            "session_key": "agent:main:main",
            "parent_session_id": None,
            "agent_id": "nas-openclaw",
            "repo_owner": "example",
            "repo_name": "nexus-demo",
            "branch": "main",
            "dedup_key": "span-canonical",
        },
        {
            "trace_id": "trace-key",
            "span_id": "span-key",
            "parent_span_id": None,
            "name": "turn",
            "service_name": "agentweave",
            "start_time": now + timedelta(seconds=1),
            "end_time": now + timedelta(seconds=1, milliseconds=10),
            "duration_ms": 10.0,
            "harness": "openclaw",
            "session_id": None,
            "session_key": "agent:key-only:main",
            "parent_session_id": None,
            "agent_id": "nas-openclaw",
            "repo_owner": "example",
            "repo_name": "nexus-demo",
            "branch": "main",
            "dedup_key": "span-key",
        },
        {
            "trace_id": "trace-parent",
            "span_id": "span-parent",
            "parent_span_id": None,
            "name": "child",
            "service_name": "agentweave",
            "start_time": now + timedelta(seconds=2),
            "end_time": now + timedelta(seconds=2, milliseconds=10),
            "duration_ms": 10.0,
            "harness": "openclaw",
            "session_id": "child-session-uuid",
            "session_key": "agent:child:main",
            "parent_session_id": "parent-session-uuid",
            "agent_id": "nas-openclaw",
            "repo_owner": "example",
            "repo_name": "nexus-demo",
            "branch": "main",
            "dedup_key": "span-parent",
        },
        {
            "trace_id": "trace-unmatched",
            "span_id": "span-unmatched",
            "parent_span_id": None,
            "name": "turn",
            "service_name": "agentweave",
            "start_time": now + timedelta(seconds=3),
            "end_time": now + timedelta(seconds=3, milliseconds=10),
            "duration_ms": 10.0,
            "harness": "openclaw",
            "session_id": "unknown-session",
            "session_key": "agent:unknown:main",
            "parent_session_id": None,
            "agent_id": "nas-openclaw",
            "repo_owner": "other",
            "repo_name": "unmatched-demo",
            "branch": "main",
            "dedup_key": "span-unmatched",
        },
    ]
    span_dir = parquet_dir / "spans" / "date=2026-05-31"
    span_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                field.name: pa.array(
                    [row.get(field.name) for row in span_rows], type=field.type
                )
                for field in span_schema
            },
            schema=span_schema,
        ),
        span_dir / "part-spans.parquet",
    )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    try:
        rows = con.execute("""
            SELECT span_id, event_session_id, event_session_key, link_method, link_confidence
            FROM openclaw_span_links
            WHERE span_id LIKE 'span-%'
            ORDER BY span_id
            """).fetchall()
    finally:
        con.close()

    assert rows == [
        (
            "span-canonical",
            "session-uuid-1",
            "agent:main:main",
            "canonical_session_id",
            "exact",
        ),
        (
            "span-key",
            "legacy-session-without-uuid",
            "agent:key-only:main",
            "session_key",
            "strong",
        ),
        (
            "span-parent",
            "parent-session-uuid",
            None,
            "parent_session_id",
            "strong",
        ),
        ("span-unmatched", None, None, "unmatched", "none"),
    ]


def test_openclaw_span_links_tolerates_bad_json_and_ambiguous_fallbacks(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)

    now = datetime(2026, 5, 31, 13, tzinfo=timezone.utc)
    event_schema = pa.schema(
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
    event_rows = [
        {
            "id": "evt-null-trace",
            "session_id": "session-null-trace",
            "agent_id": "nas-openclaw",
            "task_id": "task-1",
            "timestamp": now,
            "event_type": "user_turn",
            "role": "user",
            "content": "canonical event",
            "repo_owner": "example",
            "repo_name": "nexus-demo",
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "evt-null-trace",
            "raw_data": json.dumps({"harness": "openclaw"}),
        },
        {
            "id": "evt-unique-fallback",
            "session_id": "session-unique-fallback",
            "agent_id": "nas-openclaw",
            "task_id": "task-1",
            "timestamp": now + timedelta(seconds=1),
            "event_type": "assistant_turn",
            "role": "assistant",
            "content": "fallback event",
            "repo_owner": "example",
            "repo_name": "unique-project",
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "evt-unique-fallback",
            "raw_data": json.dumps({"harness": "openclaw"}),
        },
        {
            "id": "evt-ambiguous-a",
            "session_id": "session-ambiguous-a",
            "agent_id": "nas-openclaw",
            "task_id": "task-1",
            "timestamp": now + timedelta(seconds=2),
            "event_type": "assistant_turn",
            "role": "assistant",
            "content": "ambiguous event a",
            "repo_owner": "example",
            "repo_name": "ambiguous-project",
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "evt-ambiguous-a",
            "raw_data": json.dumps({"harness": "openclaw"}),
        },
        {
            "id": "evt-ambiguous-b",
            "session_id": "session-ambiguous-b",
            "agent_id": "nas-openclaw",
            "task_id": "task-1",
            "timestamp": now + timedelta(seconds=3),
            "event_type": "assistant_turn",
            "role": "assistant",
            "content": "ambiguous event b",
            "repo_owner": "example",
            "repo_name": "ambiguous-project",
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "evt-ambiguous-b",
            "raw_data": json.dumps({"harness": "openclaw"}),
        },
        {
            "id": "evt-bad-json",
            "session_id": "bad-json-session",
            "agent_id": "nas-openclaw",
            "task_id": "task-1",
            "timestamp": now + timedelta(seconds=4),
            "event_type": "assistant_turn",
            "role": "assistant",
            "content": "bad json should not break view",
            "repo_owner": "example",
            "repo_name": "nexus-demo",
            "branch": "main",
            "principal_id": "arnab",
            "dedup_key": "evt-bad-json",
            "raw_data": "not json",
        },
    ]
    event_dir = (
        parquet_dir / "agent_events" / "date=2026-05-31" / "agent_id=nas-openclaw"
    )
    event_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                field.name: pa.array(
                    [row.get(field.name) for row in event_rows], type=field.type
                )
                for field in event_schema
            },
            schema=event_schema,
        ),
        event_dir / "part-openclaw-extra.parquet",
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
            ("parent_session_id", pa.string()),
            ("agent_id", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("dedup_key", pa.string()),
        ]
    )
    span_rows = [
        {
            "trace_id": None,
            "span_id": "span-null-trace",
            "parent_span_id": None,
            "name": "turn",
            "service_name": "agentweave",
            "start_time": now,
            "end_time": now + timedelta(milliseconds=10),
            "duration_ms": 10.0,
            "harness": "openclaw",
            "session_id": "session-null-trace",
            "session_key": None,
            "parent_session_id": None,
            "agent_id": "nas-openclaw",
            "repo_owner": "example",
            "repo_name": "nexus-demo",
            "branch": "main",
            "dedup_key": "span-null-trace",
        },
        {
            "trace_id": "trace-unique-fallback",
            "span_id": "span-unique-fallback",
            "parent_span_id": None,
            "name": "turn",
            "service_name": "agentweave",
            "start_time": now + timedelta(seconds=1),
            "end_time": now + timedelta(seconds=1, milliseconds=10),
            "duration_ms": 10.0,
            "harness": "openclaw",
            "session_id": None,
            "session_key": None,
            "parent_session_id": None,
            "agent_id": "nas-openclaw",
            "repo_owner": "example",
            "repo_name": "unique-project",
            "branch": "main",
            "dedup_key": "span-unique-fallback",
        },
        {
            "trace_id": "trace-ambiguous-fallback",
            "span_id": "span-ambiguous-fallback",
            "parent_span_id": None,
            "name": "turn",
            "service_name": "agentweave",
            "start_time": now + timedelta(seconds=2),
            "end_time": now + timedelta(seconds=2, milliseconds=10),
            "duration_ms": 10.0,
            "harness": "openclaw",
            "session_id": None,
            "session_key": None,
            "parent_session_id": None,
            "agent_id": "nas-openclaw",
            "repo_owner": "example",
            "repo_name": "ambiguous-project",
            "branch": "main",
            "dedup_key": "span-ambiguous-fallback",
        },
    ]
    span_dir = parquet_dir / "spans" / "date=2026-05-31"
    span_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                field.name: pa.array(
                    [row.get(field.name) for row in span_rows], type=field.type
                )
                for field in span_schema
            },
            schema=span_schema,
        ),
        span_dir / "part-spans-extra.parquet",
    )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    try:
        rows = con.execute("""
            SELECT span_id, event_session_id, link_method, link_confidence
            FROM openclaw_span_links
            WHERE span_id IN (
              'span-null-trace',
              'span-unique-fallback',
              'span-ambiguous-fallback'
            )
            ORDER BY span_id, link_method
            """).fetchall()
    finally:
        con.close()

    assert rows == [
        ("span-ambiguous-fallback", None, "unmatched", "none"),
        ("span-null-trace", "session-null-trace", "canonical_session_id", "exact"),
        (
            "span-unique-fallback",
            "session-unique-fallback",
            "agent_day_project",
            "weak",
        ),
    ]


def test_openclaw_span_links_handles_span_only_and_native_only_inputs(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)

    now = datetime(2026, 5, 31, 14, tzinfo=timezone.utc)
    event_schema = pa.schema(
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
    event_dir = (
        parquet_dir / "agent_events" / "date=2026-05-31" / "agent_id=nas-openclaw"
    )
    event_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": ["evt-native-only"],
                "session_id": ["native-only-session"],
                "agent_id": ["nas-openclaw"],
                "task_id": ["task-native-only"],
                "timestamp": [now],
                "event_type": ["user_turn"],
                "role": ["user"],
                "content": ["native stream without spans"],
                "repo_owner": ["example"],
                "repo_name": ["native-only"],
                "branch": ["main"],
                "principal_id": ["arnab"],
                "dedup_key": ["evt-native-only"],
                "raw_data": [
                    json.dumps(
                        {"harness": "openclaw", "session_key": "agent:native:main"}
                    )
                ],
            },
            schema=event_schema,
        ),
        event_dir / "part-native-only.parquet",
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
            ("parent_session_id", pa.string()),
            ("agent_id", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("dedup_key", pa.string()),
        ]
    )
    span_dir = parquet_dir / "spans" / "date=2026-05-31"
    span_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "trace_id": ["trace-span-only"],
                "span_id": ["span-only"],
                "parent_span_id": [None],
                "name": ["agent.turn"],
                "service_name": ["agentweave"],
                "start_time": [now + timedelta(seconds=30)],
                "end_time": [now + timedelta(seconds=30, milliseconds=10)],
                "duration_ms": [10.0],
                "harness": ["openclaw"],
                "session_id": ["span-only-session"],
                "session_key": ["agent:span-only:main"],
                "parent_session_id": [None],
                "agent_id": ["nas-openclaw"],
                "repo_owner": ["example"],
                "repo_name": ["span-only"],
                "branch": ["main"],
                "dedup_key": ["span-only"],
            },
            schema=span_schema,
        ),
        span_dir / "part-span-only.parquet",
    )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    try:
        span_rows = con.execute("""
            SELECT span_id, event_session_id, link_method, link_confidence
            FROM openclaw_span_links
            WHERE span_id = 'span-only'
            """).fetchall()
        native_event_count = con.execute("""
            SELECT count(*)
            FROM agent_events
            WHERE session_id = 'native-only-session'
            """).fetchone()[0]
    finally:
        con.close()

    assert span_rows == [("span-only", None, "unmatched", "none")]
    assert native_event_count == 1
