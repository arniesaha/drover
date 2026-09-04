"""Materialize one compatibility usage row from independently observed sources."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb

from drover.server.harness.usage import TokenTotals

SOURCE_HARNESS_EVENTS = "harness_events"
SOURCE_NATIVE_AGENT_EVENTS = "native_agent_events"
SOURCE_UNOBSERVED = "unobserved"


def _source_usage_id(session_id: str, source: str) -> str:
    return f"{source}:{session_id}"


_UPSERT_SOURCE_SQL = """
INSERT INTO session_usage_sources
  (source_usage_id, session_id, source, host_id, harness, input_tokens,
   output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,
   turn_count, exact, usage_observed, source_seq, source_event_count, observed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (source_usage_id) DO UPDATE SET
  host_id = excluded.host_id,
  harness = excluded.harness,
  input_tokens = excluded.input_tokens,
  output_tokens = excluded.output_tokens,
  cache_read_tokens = excluded.cache_read_tokens,
  cache_write_tokens = excluded.cache_write_tokens,
  reasoning_tokens = excluded.reasoning_tokens,
  turn_count = excluded.turn_count,
  exact = excluded.exact,
  usage_observed = excluded.usage_observed,
  source_seq = excluded.source_seq,
  source_event_count = excluded.source_event_count,
  observed_at = excluded.observed_at
"""

_SELECT_CURRENT_SOURCE_SQL = """
SELECT source, host_id, harness, input_tokens, output_tokens,
       cache_read_tokens, cache_write_tokens, reasoning_tokens, turn_count,
       exact, usage_observed, source_seq, source_event_count, observed_at
FROM session_usage_sources
WHERE session_id = ?
ORDER BY CASE
           WHEN source = ? AND usage_observed THEN 0
           WHEN source = ? AND usage_observed THEN 1
           WHEN source = ? THEN 2
           ELSE 3
         END,
         observed_at DESC,
         source
LIMIT 1
"""

_UPSERT_COMPATIBILITY_SQL = """
INSERT INTO session_usage
  (session_id, host_id, harness, input_tokens, output_tokens,
   cache_read_tokens, cache_write_tokens, reasoning_tokens, turn_count,
   exact, source, source_seq, source_event_count, observed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (session_id) DO UPDATE SET
  host_id = excluded.host_id,
  harness = excluded.harness,
  input_tokens = excluded.input_tokens,
  output_tokens = excluded.output_tokens,
  cache_read_tokens = excluded.cache_read_tokens,
  cache_write_tokens = excluded.cache_write_tokens,
  reasoning_tokens = excluded.reasoning_tokens,
  turn_count = excluded.turn_count,
  exact = excluded.exact,
  source = excluded.source,
  source_seq = excluded.source_seq,
  source_event_count = excluded.source_event_count,
  observed_at = excluded.observed_at
"""


def upsert_source_usage(
    con: duckdb.DuckDBPyConnection,
    *,
    session_id: str,
    source: str,
    usage: TokenTotals,
    turn_count: int,
    exact: bool,
    source_seq: int,
    source_event_count: int,
    host_id: str | None = None,
    harness: str | None = None,
    observed_at: datetime | None = None,
) -> None:
    """Persist one producer's usage, then refresh the selected session total."""
    observed_at = observed_at or datetime.now(timezone.utc).replace(tzinfo=None)
    con.execute(
        _UPSERT_SOURCE_SQL,
        [
            _source_usage_id(session_id, source),
            session_id,
            source,
            host_id,
            harness,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            usage.reasoning_tokens,
            turn_count,
            exact,
            usage.observed,
            source_seq,
            source_event_count,
            observed_at,
        ],
    )
    row = con.execute(
        _SELECT_CURRENT_SOURCE_SQL,
        [
            session_id,
            SOURCE_HARNESS_EVENTS,
            SOURCE_NATIVE_AGENT_EVENTS,
            SOURCE_HARNESS_EVENTS,
        ],
    ).fetchone()
    if row is None:
        return
    selected_source = str(row[0]) if bool(row[10]) else SOURCE_UNOBSERVED
    con.execute(
        _UPSERT_COMPATIBILITY_SQL,
        [session_id, *row[1:10], selected_source, *row[11:14]],
    )
