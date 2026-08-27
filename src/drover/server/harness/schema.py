"""DuckDB tables for Drover harness control-plane state."""

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
  model_catalogs_json VARCHAR NOT NULL DEFAULT '{}',
  agent_version     VARCHAR,
  update_json       VARCHAR,
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


#: The one field `HarnessEvent.wire_payload` strips on its way out. A replay
#: therefore stores a slimmer copy than the original push did, so two rows for
#: one event can differ by this alone and still be the same event.
_STRIPPED_PAYLOAD_FIELD = "aggregated_output"

#: Removes the field above and the row's own identifier, which is the only other
#: thing that legitimately differs between an original and its replay. Anything
#: still different after this is a different event.
#: The trailing `\\s*` matters: without it the separator's whitespace survives,
#: so a stripped payload reads `{ "command": ...}` and the copy that never had
#: the field reads `{"command": ...}`. Those are the same event and would not
#: compare equal. Whitespace is consumed here rather than globally, because
#: stripping it everywhere would also collapse spaces inside string values and
#: make two genuinely different payloads look identical.
_NORMALISED_BODY_SQL = (
    "replace(regexp_replace(payload_json, "
    "'\"aggregated_output\"\\s*:\\s*\"(\\\\.|[^\"\\\\])*\",?\\s*', '', 'g'), "
    "event_id, '')"
)


@dataclass(frozen=True)
class DuplicateEventAudit:
    """What a dedupe pass would do, without doing it."""

    duplicate_groups: int
    collapsible_groups: int
    removable_rows: int
    divergent_groups: int
    divergent_examples: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class DuplicateEventMigration:
    """What a dedupe pass did."""

    collapsed_groups: int
    removed_rows: int
    divergent_groups: int


def _duplicate_groups_sql(where: str = "") -> str:
    return f"""
        WITH norm AS (
            SELECT session_id, seq, event_type, created_at, event_id,
                   {_NORMALISED_BODY_SQL} AS body
              FROM harness_events
             WHERE seq IS NOT NULL
        ),
        grouped AS (
            SELECT session_id, seq,
                   count(*) AS rows_in_group,
                   count(DISTINCT body) AS bodies,
                   count(DISTINCT event_type) AS types,
                   count(DISTINCT created_at) AS stamps
              FROM norm
             GROUP BY session_id, seq
            HAVING count(*) > 1
        )
        SELECT session_id, seq, rows_in_group,
               (bodies = 1 AND types = 1 AND stamps = 1) AS collapsible
          FROM grouped{where}
    """


def audit_duplicate_harness_events(
    con: duckdb.DuckDBPyConnection,
) -> DuplicateEventAudit:
    """Count the duplicate rows a dedupe would remove. Reads only.

    Structured events were stored twice for as long as the host and the hub
    disagreed on an event's identifier: the replay path offered central an id it
    had never seen, so `ON CONFLICT DO NOTHING` found no conflict (drover#270,
    fixed for new events in drover#275). This reports the history that fix does
    not reach.

    A group is collapsible only when its rows agree on `event_type`,
    `created_at` and the normalised body. Anything else is two different events
    that happen to share a sequence number, and is counted separately rather
    than removed -- on the live hub that is 30 groups, all at `seq = 1`, where a
    second producer started numbering from zero again.
    """
    rows = con.execute(_duplicate_groups_sql()).fetchall()
    collapsible = [row for row in rows if row[3]]
    divergent = [row for row in rows if not row[3]]
    return DuplicateEventAudit(
        duplicate_groups=len(rows),
        collapsible_groups=len(collapsible),
        removable_rows=sum(int(row[2]) - 1 for row in collapsible),
        divergent_groups=len(divergent),
        divergent_examples=tuple((str(row[0]), int(row[1])) for row in divergent[:10]),
    )


def migrate_duplicate_harness_events(
    con: duckdb.DuckDBPyConnection,
) -> DuplicateEventMigration:
    """Collapse each duplicate group to one row. Idempotent.

    Keeps the copy `wire_payload` would produce -- the one without
    `aggregated_output` -- because that is the form the system already converges
    on, and because the field is dead weight the adapter stopped writing. Ties
    break on the smallest `event_id` so a re-run is a no-op rather than a
    reshuffle.

    Nothing outside `harness_events` references `event_id`, so removing the
    extra row of a pair is referentially safe.
    """
    audit = audit_duplicate_harness_events(con)
    if not audit.collapsible_groups:
        return DuplicateEventMigration(0, 0, audit.divergent_groups)

    con.execute("BEGIN TRANSACTION")
    try:
        removed = con.execute(f"""
            DELETE FROM harness_events
             WHERE event_id IN (
                WITH collapsible AS ({_duplicate_groups_sql(" WHERE bodies = 1 AND types = 1 AND stamps = 1")}),
                ranked AS (
                    SELECT e.event_id,
                           row_number() OVER (
                               PARTITION BY e.session_id, e.seq
                               ORDER BY length(e.payload_json), e.event_id
                           ) AS rank
                      FROM harness_events e
                      JOIN collapsible c
                        ON c.session_id = e.session_id AND c.seq = e.seq
                )
                SELECT event_id FROM ranked WHERE rank > 1
             )
            RETURNING event_id
            """).fetchall()
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return DuplicateEventMigration(
        collapsed_groups=audit.collapsible_groups,
        removed_rows=len(removed),
        divergent_groups=audit.divergent_groups,
    )


def bootstrap_harness_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create Drover harness control-plane tables. Idempotent."""
    con.execute(_HARNESS_HOSTS_DDL)
    _ensure_harness_columns(
        con,
        "harness_hosts",
        {
            "connection_kind": "VARCHAR",
            "agent_version": "VARCHAR",
            "model_catalogs_json": "VARCHAR NOT NULL DEFAULT '{}'",
            "update_json": "VARCHAR",
        },
    )
    con.execute(_HARNESS_SESSIONS_DDL)
    # Drop the client-key index before migrating columns and recreate it after.
    # DuckDB refuses to drop a column when an index depends on any column after
    # it, so an index left in place turns every later column migration on this
    # table into a catalog error. Cheap to rebuild; expensive to discover.
    con.execute("DROP INDEX IF EXISTS harness_sessions_client_key")
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
            "recap_reconcile_needed": "BOOLEAN DEFAULT FALSE",
            "client_session_id": "VARCHAR",
        },
    )
    # A caller-supplied idempotency key, so a create whose response was lost
    # can be resolved by asking rather than by guessing (drover#268).
    #
    # NULLs are distinct in a unique index, and here that is exactly right:
    # only sessions that opt in by supplying a key are fenced, and the many
    # that do not coexist untouched. drover#256 was the same SQL semantics
    # being wrong, because that fence was meant to cover every row and a
    # nullable column meant it covered almost none. The difference is intent,
    # not the rule.
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS harness_sessions_client_key "
        "ON harness_sessions (client_session_id)"
    )
    con.execute(_HARNESS_EVENTS_DDL)
    # Dropped before the column migration below and recreated after, because
    # DuckDB refuses to drop a column an index depends on positionally.
    con.execute("DROP INDEX IF EXISTS harness_events_dedup_key")
    _ensure_harness_columns(
        con,
        "harness_events",
        {
            "normalized_type": "VARCHAR",
            "normalized_source": "VARCHAR",
            "content_preview": "VARCHAR",
            "seq": "INTEGER",
            "dedup_key": "VARCHAR",
        },
    )
    # The fence that actually identifies an event. `event_id` is minted per
    # insert, so a replay arriving under a fresh one never conflicted and every
    # harnessd restart re-inserted the whole history (drover#280).
    #
    # NULLs stay distinct here on purpose: rows written before this column
    # existed have none, and they must not collide with each other. That makes
    # backfilling part of the migration rather than optional -- an unbackfilled
    # row is still unfenced.
    con.execute("DROP INDEX IF EXISTS harness_events_dedup_key")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS harness_events_dedup_key "
        "ON harness_events (dedup_key)"
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
            # DuckDB accepts NOT NULL in CREATE TABLE but not in ADD COLUMN.
            # Add the defaulted column first (which backfills existing rows),
            # then apply the constraint as a second additive migration step.
            if " NOT NULL" in ddl_type:
                con.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} "
                    f"{ddl_type.replace(' NOT NULL', '')}"
                )
                con.execute(f"ALTER TABLE {table} ALTER COLUMN {name} SET NOT NULL")
            else:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")
