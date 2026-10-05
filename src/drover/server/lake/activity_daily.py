"""Compact per-day event facts used by the DuckLake cockpit read path."""

from __future__ import annotations

from collections.abc import Iterable

ACTIVITY_DAILY_SCHEMA = {
    "date": "VARCHAR",
    "session_id": "VARCHAR",
    "agent_id": "VARCHAR",
    "repo_owner": "VARCHAR",
    "repo_name": "VARCHAR",
    "source": "VARCHAR",
    "event_count": "BIGINT",
    "first_event_at": "TIMESTAMPTZ",
    "last_event_at": "TIMESTAMPTZ",
    "is_claude_mem_observer": "BOOLEAN",
}


def refresh_activity_daily(con, dates: Iterable[str]) -> None:
    """Replace affected daily facts in the caller's current lake transaction.

    ``agent_events`` is already canonical at this point, so this reduction has
    no global dedupe window. Keeping the delete and insert in the export
    transaction means readers see either both a new event and its rollup, or
    neither of them.
    """
    selected = sorted({str(value) for value in dates if value is not None})
    if not selected:
        return
    placeholders = ", ".join("?" for _ in selected)
    con.execute(
        f"DELETE FROM lake.activity_daily WHERE date IN ({placeholders})", selected
    )
    con.execute(
        f"""
        INSERT INTO lake.activity_daily
        SELECT
          date,
          session_id,
          agent_id,
          COALESCE(
            repo_owner,
            CASE WHEN json_valid(raw_data)
              THEN json_extract_string(raw_data, '$._repo_owner') END
          ) AS repo_owner,
          COALESCE(
            repo_name,
            CASE WHEN json_valid(raw_data)
              THEN json_extract_string(raw_data, '$._repo_name') END
          ) AS repo_name,
          CASE WHEN dedup_key_source='outbox' OR
                    (CASE WHEN json_valid(raw_data)
                      THEN json_extract_string(raw_data, '$.source') END)='control'
            THEN 'control' ELSE 'native' END AS source,
          count(*) AS event_count,
          min(TRY_CAST(timestamp AS TIMESTAMPTZ)) AS first_event_at,
          max(TRY_CAST(timestamp AS TIMESTAMPTZ)) AS last_event_at,
          COALESCE(bool_or(
            CASE WHEN json_valid(raw_data) THEN ends_with(
              rtrim(COALESCE(
                NULLIF(trim(json_extract_string(raw_data, '$.cwd')), ''),
                NULLIF(trim(json_extract_string(raw_data, '$.currentWorkingDirectory')), ''),
                NULLIF(trim(json_extract_string(raw_data, '$.working_directory')), ''),
                NULLIF(trim(json_extract_string(raw_data, '$.workspaceDir')), ''),
                ''
              ), '/'),
              '/claude/mem/observer/sessions'
            ) ELSE FALSE END
          ), FALSE) AS is_claude_mem_observer
        FROM lake.agent_events
        WHERE date IN ({placeholders}) AND session_id IS NOT NULL
        GROUP BY 1, 2, 3, 4, 5, 6
        """,
        selected,
    )
