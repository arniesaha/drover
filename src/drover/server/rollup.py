"""Refresh derived columns on the ``tasks`` table from agent_events / spans.

``tasks.session_count`` and ``tasks.total_cost_usd`` are declared in the
schema but no write path keeps them current. We compute them here in one
shot — cheap at the current scale (≤80 tasks, ≤500k events) and the SQL
is atomic.

Also fills ``tasks.repo_owner`` / ``repo_name`` / ``branch`` if newer
attributed events surfaced after the task row was originally created
(common after a one-time attribution backfill).
"""

from __future__ import annotations

from typing import Iterable

import duckdb

from drover.event_identity import canonical_agent_events_cte

_ROLLUP_SQL = f"""
WITH {canonical_agent_events_cte()}
UPDATE tasks AS t SET
  session_count  = COALESCE((
    SELECT COUNT(DISTINCT session_id)
      FROM canonical_agent_events ae
     WHERE ae.task_id = t.task_id
  ), 0),
  total_cost_usd = COALESCE((
    SELECT SUM(cost_usd)
      FROM spans s
     WHERE s.task_id = t.task_id
  ), 0.0),
  repo_owner = COALESCE(t.repo_owner, (
    SELECT any_value(ae.repo_owner)
      FROM canonical_agent_events ae
     WHERE ae.task_id = t.task_id AND ae.repo_owner IS NOT NULL
  )),
  repo_name = COALESCE(t.repo_name, (
    SELECT any_value(ae.repo_name)
      FROM canonical_agent_events ae
     WHERE ae.task_id = t.task_id AND ae.repo_name IS NOT NULL
  )),
  branch = COALESCE(t.branch, (
    SELECT any_value(ae.branch)
      FROM canonical_agent_events ae
     WHERE ae.task_id = t.task_id AND ae.branch IS NOT NULL
  ))
WHERE t.task_id IS NOT NULL
"""


def rollup_tasks(
    con: duckdb.DuckDBPyConnection,
    *,
    task_ids: Iterable[str] | None = None,
    dates: Iterable[str] | None = None,
) -> int:
    """Refresh derived columns on task rows.

    When ``task_ids`` is provided, only touched tasks are updated. This keeps
    the always-on ingest path from running broad historical rollups after every
    small incoming batch. Omitting ``task_ids`` preserves the full-refresh CLI
    behavior.
    """
    ids = sorted({task_id for task_id in (task_ids or []) if task_id})
    bounded_dates = sorted({date for date in (dates or []) if date})
    if ids:
        if bounded_dates:
            event_source_sql = "\nUNION ALL\n".join(
                "SELECT * FROM agent_events_for_date(?)" for _ in bounded_dates
            )
            total_cost_sql = "t.total_cost_usd"
            params = [*bounded_dates, ids]
        else:
            event_source_sql = "SELECT * FROM agent_events"
            total_cost_sql = "COALESCE((SELECT SUM(cost_usd) FROM spans s WHERE s.task_id = t.task_id), 0.0)"
            params = [ids]
        con.execute(
            f"""
            WITH bounded_agent_events AS (
              {event_source_sql}
            )
            UPDATE tasks AS t SET
              session_count = greatest(t.session_count, COALESCE((
                SELECT COUNT(DISTINCT session_id)
                  FROM bounded_agent_events ae
                 WHERE ae.task_id = t.task_id
              ), 0)),
              total_cost_usd = {total_cost_sql},
              repo_owner = COALESCE(t.repo_owner, (
                SELECT any_value(ae.repo_owner)
                  FROM bounded_agent_events ae
                 WHERE ae.task_id = t.task_id AND ae.repo_owner IS NOT NULL
              )),
              repo_name = COALESCE(t.repo_name, (
                SELECT any_value(ae.repo_name)
                  FROM bounded_agent_events ae
                 WHERE ae.task_id = t.task_id AND ae.repo_name IS NOT NULL
              )),
              branch = COALESCE(t.branch, (
                SELECT any_value(ae.branch)
                  FROM bounded_agent_events ae
                 WHERE ae.task_id = t.task_id AND ae.branch IS NOT NULL
              ))
            WHERE t.task_id = ANY(?::VARCHAR[])
            """,
            params,
        )
        return len(ids)
    con.execute(_ROLLUP_SQL)
    row = con.execute("SELECT COUNT(*) FROM tasks").fetchone()
    return row[0] if row else 0
