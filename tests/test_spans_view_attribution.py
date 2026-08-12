"""Cross-namespace span attribution via the spans view (#52).

AgentWeave spans use a different session_id / task_id namespace than
Claude Code agent_events, so the session-level join in the spans view
rarely fires. The agent_day_repos CTE adds a fallback: spans inherit
``repo_owner`` from whatever repo the same agent_id was most active on
that day in agent_events.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from drover.schema import bootstrap

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
        ("attributes_json", pa.string()),
        ("dedup_key", pa.string()),
    ]
)


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _write_event(parquet_dir: Path, **row) -> None:
    date = row["timestamp"].date().isoformat()
    out = parquet_dir / "agent_events" / f"date={date}" / f"agent_id={row['agent_id']}"
    out.mkdir(parents=True, exist_ok=True)
    cols = {f.name: pa.array([row.get(f.name)], type=f.type) for f in _EVENT_SCHEMA}
    pq.write_table(pa.table(cols, schema=_EVENT_SCHEMA), out / f"{row['id']}.parquet")


def _write_span(parquet_dir: Path, **row) -> None:
    date = row["start_time"].date().isoformat()
    out = parquet_dir / "spans" / f"date={date}"
    out.mkdir(parents=True, exist_ok=True)
    cols = {f.name: pa.array([row.get(f.name)], type=f.type) for f in _SPAN_SCHEMA}
    pq.write_table(
        pa.table(cols, schema=_SPAN_SCHEMA), out / f"{row['span_id']}.parquet"
    )


def test_spans_view_derives_repo_from_safe_agentweave_cwd_attr(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    when = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)

    _write_span(
        parquet_dir,
        trace_id="trace-attr-cwd",
        span_id="span-attr-cwd",
        parent_span_id=None,
        name="agent.turn",
        service_name="agentweave-proxy",
        start_time=when,
        end_time=when.replace(minute=1),
        duration_ms=60_000.0,
        session_id="openclaw-session",
        task_id="task",
        agent_id="nas-openclaw",
        cost_usd=0.01,
        attributes_json=json.dumps(
            {
                "prov.harness": "openclaw",
                "prov.cwd": "/home/Arnab/dev/openclaw/plugins/cursor",
                "prov.project": "OpenClaw",
            }
        ),
        dedup_key="span-attr-cwd",
    )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        row = con.execute("""
            SELECT project, repo_owner, repo_name
            FROM spans
            WHERE span_id = 'span-attr-cwd'
            """).fetchone()
    finally:
        con.close()

    assert row == ("OpenClaw", None, None)


def test_spans_view_treats_negative_cost_sentinel_as_unavailable(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    when = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    _write_span(
        parquet_dir,
        trace_id="trace-negative-cost",
        span_id="span-negative-cost",
        parent_span_id=None,
        name="llm.claude-opus-5",
        service_name="agentweave-proxy",
        start_time=when,
        end_time=when,
        duration_ms=1.0,
        session_id="session-negative-cost",
        task_id=None,
        agent_id="nas-claude",
        cost_usd=-1.0,
        attributes_json=json.dumps({"cost.usd": -1.0}),
        dedup_key="span-negative-cost",
    )
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        normalized = con.execute(
            "SELECT cost_usd FROM spans WHERE span_id='span-negative-cost'"
        ).fetchone()
        raw = con.execute(
            """
            SELECT cost_usd
            FROM read_parquet(?, hive_partitioning=true, union_by_name=true)
            WHERE span_id='span-negative-cost'
            """,
            [str(parquet_dir / "spans" / "date=*" / "*.parquet")],
        ).fetchone()
    finally:
        con.close()

    assert raw == (-1.0,)
    assert normalized == (None,)


def test_span_inherits_repo_from_same_agent_same_day(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    when = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)

    # An agent_event that pins (nas-claude, 2026-05-12) → arniesaha/portfolio
    _write_event(
        parquet_dir,
        id="ae-1",
        session_id="local-uuid-1",
        agent_id="nas-claude",
        task_id="t1",
        timestamp=when,
        event_type="user_message",
        role="user",
        content="working on portfolio",
        repo_owner="arniesaha",
        repo_name="portfolio",
        branch="main",
        principal_id="arnab",
        dedup_key="ae1",
        raw_data="{}",
    )
    # A span from the same agent on the same day, but with an AgentWeave-shape
    # session_id that doesn't match anything in agent_events
    _write_span(
        parquet_dir,
        trace_id="trace-1",
        span_id="span-1",
        parent_span_id=None,
        name="llm_call",
        service_name="claude-code",
        start_time=when.replace(hour=11),
        end_time=when.replace(hour=11, minute=1),
        duration_ms=60_000.0,
        session_id="agent:main:cron:abc:run:xyz",
        task_id="aw-task",
        agent_id="nas-claude",
        cost_usd=0.42,
        dedup_key="span1",
    )

    # Rebuild the view so it sees the new parquet partitions.
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        row = con.execute(
            "SELECT repo_owner, repo_name, branch FROM spans_enriched WHERE span_id='span-1'"
        ).fetchone()
    finally:
        con.close()

    assert row == ("arniesaha", "portfolio", "main")


def test_spans_view_canonicalizes_historical_agent_ids_for_read_time_join(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    when = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)

    _write_event(
        parquet_dir,
        id="ae-1",
        session_id="local-uuid-1",
        agent_id="macmini-claude",
        task_id="t1",
        timestamp=when,
        event_type="user_message",
        role="user",
        content="working on nexus",
        repo_owner="arniesaha",
        repo_name="nexus",
        branch="main",
        principal_id="arnab",
        dedup_key="ae1",
        raw_data="{}",
    )
    _write_span(
        parquet_dir,
        trace_id="trace-1",
        span_id="span-legacy-alias",
        parent_span_id=None,
        name="agent.turn",
        service_name="agentweave-proxy",
        start_time=when.replace(hour=11),
        end_time=when.replace(hour=11, minute=1),
        duration_ms=60_000.0,
        session_id="agentweave-session",
        task_id="aw-task",
        agent_id="claude-code-mac",
        cost_usd=0.42,
        dedup_key="span1",
    )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        row = con.execute("""
            SELECT agent_id, repo_owner, repo_name, branch
            FROM spans_enriched
            WHERE span_id = 'span-legacy-alias'
            """).fetchone()
    finally:
        con.close()

    assert row == ("macmini-claude", "arniesaha", "nexus", "main")


def test_spans_enriched_for_date_canonicalizes_historical_agent_ids(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    when = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)

    _write_event(
        parquet_dir,
        id="ae-1",
        session_id="local-uuid-1",
        agent_id="nas-openclaw",
        task_id="t1",
        timestamp=when,
        event_type="user_message",
        role="user",
        content="working on nexus",
        repo_owner="arniesaha",
        repo_name="nexus",
        branch="main",
        principal_id="arnab",
        dedup_key="ae1",
        raw_data="{}",
    )
    _write_span(
        parquet_dir,
        trace_id="trace-1",
        span_id="span-v1-subagent",
        parent_span_id=None,
        name="agent.turn",
        service_name="agentweave-proxy",
        start_time=when.replace(hour=11),
        end_time=when.replace(hour=11, minute=1),
        duration_ms=60_000.0,
        session_id="agentweave-session",
        task_id="aw-task",
        agent_id="nix-v1-subagent-v1",
        cost_usd=0.42,
        dedup_key="span1",
    )

    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        row = con.execute("""
            SELECT agent_id, repo_owner, repo_name, branch
            FROM spans_enriched_for_date('2026-05-12')
            WHERE span_id = 'span-v1-subagent'
            """).fetchone()
    finally:
        con.close()

    assert row == ("nas-openclaw", "arniesaha", "nexus", "main")


def test_span_attribution_falls_back_to_session_join_first(tmp_path: Path) -> None:
    """If the span's own session_id matches an attributed agent_event session,
    that wins over the agent-day fallback."""
    parquet_dir, duckdb_path = _seed(tmp_path)
    when = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)

    _write_event(
        parquet_dir,
        id="ae-1",
        session_id="shared-session",
        agent_id="nas-claude",
        task_id="t1",
        timestamp=when,
        event_type="user_message",
        role="user",
        content="",
        repo_owner="arniesaha",
        repo_name="exact-match-repo",
        branch="dev",
        principal_id="arnab",
        dedup_key="ae1",
        raw_data="{}",
    )
    # And a *different* attribution for the same agent on the same day,
    # to ensure the session_id join wins.
    _write_event(
        parquet_dir,
        id="ae-2",
        session_id="other-session",
        agent_id="nas-claude",
        task_id="t2",
        timestamp=when,
        event_type="user_message",
        role="user",
        content="",
        repo_owner="arniesaha",
        repo_name="other-repo",
        branch="main",
        principal_id="arnab",
        dedup_key="ae2",
        raw_data="{}",
    )
    _write_span(
        parquet_dir,
        trace_id="trace-1",
        span_id="span-1",
        parent_span_id=None,
        name="llm_call",
        service_name="claude-code",
        start_time=when.replace(hour=11),
        end_time=when.replace(hour=11, minute=1),
        duration_ms=60_000.0,
        session_id="shared-session",  # MATCHES agent_event session
        task_id="aw-task",
        agent_id="nas-claude",
        cost_usd=0.42,
        dedup_key="span1",
    )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        row = con.execute(
            "SELECT repo_owner, repo_name, branch FROM spans_enriched WHERE span_id='span-1'"
        ).fetchone()
    finally:
        con.close()

    # Session-id match wins: exact-match-repo, not other-repo.
    assert row == ("arniesaha", "exact-match-repo", "dev")


def test_spans_enriched_preserves_one_row_per_span_across_multi_day_session_events(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    when = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)

    for i, day_offset in enumerate((-1, 0, 1), start=1):
        _write_event(
            parquet_dir,
            id=f"ae-{i}",
            session_id="shared-session",
            agent_id="nas-claude",
            task_id="t1",
            timestamp=when.replace(day=when.day + day_offset),
            event_type="user_message",
            role="user",
            content="same session across dates",
            repo_owner="arniesaha",
            repo_name="nexus",
            branch="main",
            principal_id="arnab",
            dedup_key=f"ae-{i}",
            raw_data="{}",
        )
    _write_span(
        parquet_dir,
        trace_id="trace-1",
        span_id="span-1",
        parent_span_id=None,
        name="llm_call",
        service_name="claude-code",
        start_time=when,
        end_time=when.replace(minute=1),
        duration_ms=60_000.0,
        session_id="shared-session",
        task_id="aw-task",
        agent_id="nas-claude",
        cost_usd=1.0,
        dedup_key="span1",
    )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        row = con.execute(
            "SELECT count(*), sum(cost_usd), any_value(repo_owner), any_value(repo_name) "
            "FROM spans_enriched WHERE span_id='span-1'"
        ).fetchone()
    finally:
        con.close()

    assert row == (1, 1.0, "arniesaha", "nexus")


def test_spans_enriched_for_date_prunes_agent_events_to_span_partition(
    tmp_path: Path,
) -> None:
    """Bounded enrichment should not scan unrelated historical agent_events files."""

    parquet_dir, duckdb_path = _seed(tmp_path)
    when = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)

    _write_event(
        parquet_dir,
        id="ae-1",
        session_id="local-uuid-1",
        agent_id="nas-claude",
        task_id="t1",
        timestamp=when,
        event_type="user_message",
        role="user",
        content="working on portfolio",
        repo_owner="arniesaha",
        repo_name="portfolio",
        branch="main",
        principal_id="arnab",
        dedup_key="ae1",
        raw_data="{}",
    )
    _write_span(
        parquet_dir,
        trace_id="trace-1",
        span_id="span-1",
        parent_span_id=None,
        name="llm_call",
        service_name="claude-code",
        start_time=when.replace(hour=11),
        end_time=when.replace(hour=11, minute=1),
        duration_ms=60_000.0,
        session_id="agent:main:cron:abc:run:xyz",
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

    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        row = con.execute("""
            SELECT repo_owner, repo_name, branch
            FROM spans_enriched_for_date('2026-05-12')
            WHERE span_id = 'span-1'
            """).fetchone()
    finally:
        con.close()

    assert row == ("arniesaha", "portfolio", "main")


def test_spans_view_exposes_legacy_agentweave_attrs_as_columns(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    when = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)

    _write_span(
        parquet_dir,
        trace_id="trace-attrs",
        span_id="span-attrs",
        parent_span_id=None,
        name="agent.turn",
        service_name="agentweave-proxy",
        start_time=when,
        end_time=when,
        duration_ms=0.0,
        session_id=None,
        task_id=None,
        agent_id=None,
        cost_usd=None,
        attributes_json=json.dumps(
            {
                "prov.harness": "openclaw",
                "prov.session.id": "018f-openclaw-main-0001",
                "prov.session.key": "agent:main:main",
                "prov.agent.id": "nix-v1",
                "prov.project": "nix",
                "prov.cwd": "/home/Arnab/clawd",
                "prov.repository": "arniesaha/openclaw",
                "prov.repo.owner": "arniesaha",
                "prov.repo.name": "openclaw",
                "prov.git.branch": "main",
                "prov.routing.provider": "anthropic",
                "prov.routing.model": "claude-sonnet-4-5",
                "redaction.level": "preview",
                "cost.usd": "0.0123",
                "prov.llm.prompt_tokens": "10",
                "prov.llm.completion_tokens": "7",
                "tokens.cache_read": "3",
                "tokens.cache_write": "2",
            }
        ),
        dedup_key="span-attrs",
    )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        row = con.execute("""
            SELECT harness, session_id, session_key, agent_id, project, cwd,
                   repository, repo_owner, repo_name, branch, routing_provider,
                   routing_model, redaction_level, cost_usd, prompt_tokens,
                   completion_tokens, cache_read_tokens, cache_write_tokens
            FROM spans
            WHERE span_id = 'span-attrs'
            """).fetchone()
        macro_row = con.execute("""
            SELECT harness, session_id, session_key, agent_id, repo_owner, repo_name
            FROM spans_for_date('2026-05-12')
            WHERE span_id = 'span-attrs'
            """).fetchone()
    finally:
        con.close()

    assert row == (
        "openclaw",
        "018f-openclaw-main-0001",
        "agent:main:main",
        "nas-openclaw",
        "nix",
        "/home/Arnab/clawd",
        "arniesaha/openclaw",
        "arniesaha",
        "openclaw",
        "main",
        "anthropic",
        "claude-sonnet-4-5",
        "preview",
        0.0123,
        10,
        7,
        3,
        2,
    )
    assert macro_row == (
        "openclaw",
        "018f-openclaw-main-0001",
        "agent:main:main",
        "nas-openclaw",
        "arniesaha",
        "openclaw",
    )


def test_span_with_no_attribution_signal_stays_null(tmp_path: Path) -> None:
    """A span whose agent never appears in agent_events should remain
    unattributed rather than inherit something arbitrary."""
    parquet_dir, duckdb_path = _seed(tmp_path)
    when = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)

    _write_span(
        parquet_dir,
        trace_id="trace-1",
        span_id="span-orphan",
        parent_span_id=None,
        name="llm_call",
        service_name="claude-code",
        start_time=when,
        end_time=when,
        duration_ms=0.0,
        session_id="agent:main:cron:abc",
        task_id="aw-task",
        agent_id="unmapped-agent",
        cost_usd=0.0,
        dedup_key="orphan1",
    )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        row = con.execute(
            "SELECT repo_owner, repo_name, branch FROM spans_enriched WHERE span_id='span-orphan'"
        ).fetchone()
    finally:
        con.close()

    assert row == (None, None, None)
