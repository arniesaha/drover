"""DuckDB tables for Drover Meta Harness control-plane state."""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

HARNESS_TABLES = (
    "harness_hosts",
    "harness_sessions",
    "harness_events",
)

_HARNESS_HOSTS_DDL = """
CREATE TABLE IF NOT EXISTS harness_hosts (
  host_id           VARCHAR PRIMARY KEY,
  display_name      VARCHAR NOT NULL,
  kind              VARCHAR NOT NULL,
  local_url         VARCHAR,
  tailscale_url     VARCHAR,
  connection_kind   VARCHAR,
  status            VARCHAR NOT NULL,
  capabilities_json VARCHAR NOT NULL,
  last_seen_at      TIMESTAMP,
  created_at        TIMESTAMP NOT NULL DEFAULT now(),
  updated_at        TIMESTAMP NOT NULL DEFAULT now()
);
"""

_HARNESS_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS harness_sessions (
  session_id         VARCHAR PRIMARY KEY,
  host_id            VARCHAR NOT NULL,
  harness            VARCHAR NOT NULL,
  repo_owner         VARCHAR,
  repo_name          VARCHAR,
  branch             VARCHAR,
  cwd                VARCHAR,
  command            VARCHAR NOT NULL,
  status             VARCHAR NOT NULL,
  started_at         TIMESTAMP,
  updated_at         TIMESTAMP NOT NULL DEFAULT now(),
  ended_at           TIMESTAMP,
  last_error         VARCHAR,
  summary_session_id VARCHAR,
  native_session_id  VARCHAR,
  native_resume_label VARCHAR,
  source_session_id  VARCHAR,
  handoff_mode       VARCHAR,
  model              VARCHAR,
  thinking_effort    VARCHAR
);
"""

_HARNESS_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS harness_events (
  event_id          VARCHAR PRIMARY KEY,
  session_id        VARCHAR NOT NULL,
  event_type        VARCHAR NOT NULL,
  normalized_type   VARCHAR,
  normalized_source VARCHAR,
  content_preview   VARCHAR,
  payload_json      VARCHAR NOT NULL,
  created_at        TIMESTAMP NOT NULL DEFAULT now()
);
"""


@dataclass(frozen=True)
class LegacySequenceMigrationReport:
    migrated_sessions: int
    migrated_events: int
    mixed_sessions: tuple[str, ...]


@dataclass(frozen=True)
class LegacySequenceAuditReport:
    null_event_count: int
    all_null_sessions: tuple[str, ...]
    mixed_sessions: tuple[str, ...]


def audit_legacy_harness_event_sequences(
    con: duckdb.DuckDBPyConnection,
) -> LegacySequenceAuditReport:
    """Classify sessions affected by legacy null event sequences."""
    rows = con.execute("""
        SELECT
            session_id,
            count(*) FILTER (WHERE seq IS NULL) AS null_count,
            count(*) FILTER (WHERE seq IS NOT NULL) AS sequenced_count
        FROM harness_events
        GROUP BY session_id
        HAVING count(*) FILTER (WHERE seq IS NULL) > 0
        ORDER BY session_id
        """).fetchall()
    all_null = tuple(
        str(session_id) for session_id, _, sequenced in rows if not sequenced
    )
    mixed = tuple(str(session_id) for session_id, _, sequenced in rows if sequenced)
    return LegacySequenceAuditReport(
        null_event_count=sum(int(null_count) for _, null_count, _ in rows),
        all_null_sessions=all_null,
        mixed_sessions=mixed,
    )


def migrate_legacy_harness_event_sequences(
    con: duckdb.DuckDBPyConnection,
) -> LegacySequenceMigrationReport:
    audit = audit_legacy_harness_event_sequences(con)
    mixed = audit.mixed_sessions
    eligible = audit.all_null_sessions
    migrated_events = 0
    con.execute("BEGIN TRANSACTION")
    try:
        for session_id in eligible:
            event_count = con.execute(
                "SELECT count(*) FROM harness_events WHERE session_id = ?",
                [session_id],
            ).fetchone()[0]
            con.execute(
                """
                UPDATE harness_events AS target SET seq = ranked.new_seq
                FROM (
                    SELECT event_id, row_number() OVER (
                        ORDER BY created_at, event_id
                    )::INTEGER AS new_seq
                    FROM harness_events WHERE session_id = ?
                ) AS ranked
                WHERE target.event_id = ranked.event_id
                """,
                [session_id],
            )
            migrated_events += int(event_count)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return LegacySequenceMigrationReport(len(eligible), migrated_events, mixed)


def bootstrap_harness_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create Meta Harness control-plane tables. Idempotent."""
    con.execute(_HARNESS_HOSTS_DDL)
    _ensure_harness_columns(
        con,
        "harness_hosts",
        {"connection_kind": "VARCHAR"},
    )
    con.execute(_HARNESS_SESSIONS_DDL)
    _ensure_harness_columns(
        con,
        "harness_sessions",
        {
            "native_session_id": "VARCHAR",
            "native_resume_label": "VARCHAR",
            "source_session_id": "VARCHAR",
            "handoff_mode": "VARCHAR",
            "mode": "VARCHAR",
            "awaiting": "VARCHAR",
            "last_activity": "TIMESTAMP",
            "permission_mode": "VARCHAR",
            "model": "VARCHAR",
            "thinking_effort": "VARCHAR",
        },
    )
    con.execute(_HARNESS_EVENTS_DDL)
    _ensure_harness_columns(
        con,
        "harness_events",
        {
            "normalized_type": "VARCHAR",
            "normalized_source": "VARCHAR",
            "content_preview": "VARCHAR",
            "seq": "INTEGER",
        },
    )
    migrate_legacy_harness_event_sequences(con)
    # Dropped, not migrated: every row duplicated a terminal.output event
    # byte-for-byte (verified 1:1 on live data), so there is nothing here
    # that harness_events does not already hold.
    con.execute("DROP TABLE IF EXISTS harness_transcript_chunks")


def _ensure_harness_columns(
    con: duckdb.DuckDBPyConnection, table: str, columns: dict[str, str]
) -> None:
    existing = {
        row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()
    }
    for name, ddl_type in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")
