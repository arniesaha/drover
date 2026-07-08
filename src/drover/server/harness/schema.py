"""DuckDB tables for Drover Meta Harness control-plane state."""

from __future__ import annotations

import duckdb

HARNESS_TABLES = (
    "harness_hosts",
    "harness_sessions",
    "harness_events",
    "harness_transcript_chunks",
)

_HARNESS_HOSTS_DDL = """
CREATE TABLE IF NOT EXISTS harness_hosts (
  host_id           VARCHAR PRIMARY KEY,
  display_name      VARCHAR NOT NULL,
  kind              VARCHAR NOT NULL,
  local_url         VARCHAR,
  tailscale_url     VARCHAR,
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
  handoff_mode       VARCHAR
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

_HARNESS_TRANSCRIPT_CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS harness_transcript_chunks (
  chunk_id         VARCHAR PRIMARY KEY,
  session_id       VARCHAR NOT NULL,
  sequence         INTEGER NOT NULL,
  content_redacted VARCHAR NOT NULL,
  byte_count       INTEGER NOT NULL,
  created_at       TIMESTAMP NOT NULL DEFAULT now(),
  UNIQUE (session_id, sequence)
);
"""


def bootstrap_harness_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create Meta Harness control-plane tables. Idempotent."""
    con.execute(_HARNESS_HOSTS_DDL)
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
    con.execute(_HARNESS_TRANSCRIPT_CHUNKS_DDL)


def _ensure_harness_columns(
    con: duckdb.DuckDBPyConnection, table: str, columns: dict[str, str]
) -> None:
    existing = {
        row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()
    }
    for name, ddl_type in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")
