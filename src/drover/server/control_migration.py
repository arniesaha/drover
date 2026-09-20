"""Offline, fenced migration of a DuckDB control snapshot into PostgreSQL.

These functions never locate a live registry file themselves.  An operator
passes an immutable/read-only snapshot and, where needed, a separate credential
document.  Schema bootstrap is intentionally not serving readiness: only an
explicit empty initialization or a verified import marks a PostgreSQL target
ready for Task 3's API role.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb

from drover.server.control_outbox import payload_sha256
from drover.server.control_store import is_postgres_control_store
from drover.server.db import (
    CONTROL_PLANE_PRIMARY_KEYS,
    CONTROL_PLANE_TABLES,
    control_plane_connection,
)

_EXTRA_TABLES = ("control_server_identity", "control_credentials")
_ALL_TABLES = CONTROL_PLANE_TABLES + _EXTRA_TABLES

# Legacy usage writers stored their instants as naive UTC in DuckDB.  These
# columns have producer-specific provenance, so applying an operator's old
# process-wall-clock timezone would silently move real usage history.
_NAIVE_UTC_TIMESTAMP_COLUMNS = frozenset(
    {
        ("session_usage", "observed_at"),
        ("session_usage_sources", "observed_at"),
        ("native_usage_partition_totals", "observed_at"),
        ("native_usage_partition_watermarks", "source_activity_at"),
        ("native_usage_partition_watermarks", "rolled_at"),
    }
)

# These values define control-plane identity and replayability. Optional
# source columns may evolve, but their absence must not turn an import into a
# synthetic or relationally incomplete serving store.
_REQUIRED_SOURCE_COLUMNS: dict[str, frozenset[str]] = {
    "harness_hosts": frozenset(
        {"host_id", "display_name", "kind", "status", "capabilities_json"}
    ),
    "harness_sessions": frozenset(
        {"session_id", "host_id", "harness", "command", "status"}
    ),
    "harness_events": frozenset(
        {"event_id", "session_id", "event_type", "payload_json", "created_at"}
    ),
    "live_session_recaps": frozenset({"session_id", "recap_text", "source_seq"}),
    "live_recap_jobs": frozenset({"session_id", "desired_source_seq", "status"}),
    "advisory_findings": frozenset(
        {
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
            "first_seen_at",
            "last_seen_at",
            "latest_run_id",
        }
    ),
    "advisory_occurrences": frozenset(
        {"occurrence_id", "finding_id", "run_id", "outcome", "observed_at"}
    ),
    "session_usage": frozenset(
        {
            "session_id",
            "source",
            "source_seq",
            "source_event_count",
            "observed_at",
        }
    ),
    "session_usage_sources": frozenset(
        {
            "source_usage_id",
            "session_id",
            "source",
            "source_seq",
            "source_event_count",
            "observed_at",
        }
    ),
    "native_usage_partition_totals": frozenset(
        {
            "native_usage_partition_id",
            "session_id",
            "partition_date",
            "event_count",
            "observed_at",
        }
    ),
    "native_usage_partition_watermarks": frozenset(
        {"partition_date", "source_activity_at", "rolled_at"}
    ),
}


def _source_fingerprint(path: Path, credential_document: Path | None) -> str:
    digest = hashlib.sha256()
    for item in (path, credential_document):
        if item is None:
            continue
        digest.update(item.name.encode("utf-8"))
        with item.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown source timezone {name!r}") from exc


def _legacy_wall_time_to_utc(value: datetime, source_zone: ZoneInfo) -> datetime:
    """Convert a legacy naive process-local wall time without guessing DST fold."""
    if value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None:
        return value.astimezone(timezone.utc)
    first = value.replace(tzinfo=source_zone, fold=0)
    second = value.replace(tzinfo=source_zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError(f"ambiguous legacy wall time {value.isoformat()}")
    restored = (
        first.astimezone(timezone.utc).astimezone(source_zone).replace(tzinfo=None)
    )
    if restored != value:
        raise ValueError(f"nonexistent legacy wall time {value.isoformat()}")
    return first.astimezone(timezone.utc)


def _normalise_value(table: str, column: str, value: Any, source_zone: ZoneInfo) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None:
            return value.astimezone(timezone.utc)
        if (table, column) in _NAIVE_UTC_TIMESTAMP_COLUMNS:
            return value.replace(tzinfo=timezone.utc)
        return _legacy_wall_time_to_utc(value, source_zone)
    return value


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        stamp = value
        if stamp.tzinfo is None or stamp.tzinfo.utcoffset(stamp) is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _row_hash(row: dict[str, Any]) -> str:
    body = {key: _canonical_value(value) for key, value in sorted(row.items())}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_table_rows(
    snapshot: Path, source_zone: ZoneInfo, *, include_events: bool = False
) -> dict[str, list[dict[str, Any]]]:
    if not snapshot.is_file():
        raise ValueError("source_snapshot must be an explicit readable DuckDB file")
    output: dict[str, list[dict[str, Any]]] = {}
    with duckdb.connect(str(snapshot), read_only=True) as con:
        tables = {
            str(row[0])
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE'"
            ).fetchall()
        }
        missing = [table for table in CONTROL_PLANE_TABLES if table not in tables]
        if missing:
            raise ValueError(
                "legacy snapshot is missing central table(s): " + ", ".join(missing)
            )
        for table in CONTROL_PLANE_TABLES:
            if table == "harness_events" and not include_events:
                continue
            result = con.execute(f"SELECT * FROM {table}")
            columns = [item[0] for item in result.description]
            output[table] = [
                {
                    column: _normalise_value(table, column, value, source_zone)
                    for column, value in zip(columns, row)
                }
                for row in result.fetchall()
            ]
    return output


def _source_columns(snapshot: Path, table: str) -> list[str]:
    with duckdb.connect(str(snapshot), read_only=True) as con:
        result = con.execute(f"SELECT * FROM {table} LIMIT 0")
        return [item[0] for item in result.description]


def _iter_source_rows(
    snapshot: Path,
    table: str,
    source_zone: ZoneInfo,
    *,
    batch_size: int = 500,
):
    """Yield an ordered legacy table a bounded batch at a time.

    Harness history is the only control relation that can reach hundreds of
    thousands of wide envelopes, so import and canonical verification never
    build a second in-memory copy of it.
    """
    key = CONTROL_PLANE_PRIMARY_KEYS[table]
    with duckdb.connect(str(snapshot), read_only=True) as con:
        columns = _source_columns(snapshot, table)
        last_key: Any | None = None
        key_order = f'{key} COLLATE "C"' if table == "harness_events" else key
        while True:
            where = "" if last_key is None else f"WHERE {key_order} > ?"
            params = [] if last_key is None else [last_key]
            result = con.execute(
                f"SELECT * FROM {table} {where} ORDER BY {key_order} LIMIT ?",
                [*params, max(1, int(batch_size))],
            )
            rows = result.fetchall()
            if not rows:
                break
            last_key = rows[-1][columns.index(key)]
            yield [
                {
                    column: _normalise_value(table, column, value, source_zone)
                    for column, value in zip(columns, row)
                }
                for row in rows
            ]


def _credential_rows(
    document: Path | None, source_zone: ZoneInfo
) -> dict[str, list[dict[str, Any]]]:
    if document is None:
        return {"control_server_identity": [], "control_credentials": []}
    if not document.is_file():
        raise ValueError("credential_document must be an explicit readable JSON file")
    try:
        body = json.loads(document.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("credential_document must contain valid JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("credential_document must be a JSON object")
    identity = body.get("control_server_identity", {})
    if not isinstance(identity, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in identity.items()
    ):
        raise ValueError(
            "control_server_identity must map string keys to string values"
        )
    credentials = body.get("control_credentials", [])
    if not isinstance(credentials, list) or not all(
        isinstance(row, dict) for row in credentials
    ):
        raise ValueError("control_credentials must be a list of objects")
    return {
        "control_server_identity": [
            {"identity_key": key, "identity_value": value}
            for key, value in sorted(identity.items())
        ],
        "control_credentials": [
            {
                key: _normalise_value(
                    "control_credentials",
                    key,
                    (
                        datetime.fromisoformat(value.replace("Z", "+00:00"))
                        if key.endswith("_at") and isinstance(value, str)
                        else value
                    ),
                    source_zone,
                )
                for key, value in row.items()
            }
            for row in credentials
        ],
    }


def _target_columns(con: object, table: str) -> set[str]:
    return {
        str(row[0])
        for row in con.execute(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = ?
            """,
            [table],
        ).fetchall()
    }


def _validate_source_inventory(
    con: object,
    *,
    source_snapshot: Path,
    credential_rows: dict[str, list[dict[str, Any]]],
) -> None:
    """Fence every supplied source column before writing any target row."""
    for table in CONTROL_PLANE_TABLES:
        source_columns = set(_source_columns(source_snapshot, table))
        missing = sorted(_REQUIRED_SOURCE_COLUMNS[table] - source_columns)
        if missing:
            raise ValueError(
                f"source table {table} is missing required source columns: "
                + ", ".join(missing)
            )
        unsupported = sorted(source_columns - _target_columns(con, table))
        if unsupported:
            raise ValueError(
                f"source table {table} has unsupported source columns: "
                + ", ".join(unsupported)
            )
    for table in _EXTRA_TABLES:
        target_columns = _target_columns(con, table)
        supplied_columns = set().union(*(row.keys() for row in credential_rows[table]))
        unsupported = sorted(supplied_columns - target_columns)
        if unsupported:
            raise ValueError(
                f"source table {table} has unsupported source columns: "
                + ", ".join(unsupported)
            )


def _insert_rows(con: object, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    target_columns = _target_columns(con, table)
    columns = sorted(set().union(*(row.keys() for row in rows)))
    unsupported = sorted(set(columns) - target_columns)
    if unsupported:
        raise ValueError(
            f"source table {table} has unsupported source columns: "
            + ", ".join(unsupported)
        )
    if not columns:
        return
    placeholders = ", ".join("?" for _ in columns)
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        "ON CONFLICT DO NOTHING"
    )
    con.executemany(sql, [[row.get(column) for column in columns] for row in rows])


def _mark_state(
    con: object,
    *,
    state: str,
    mode: str,
    fingerprint: str | None,
    source_timezone: str | None,
    details: dict[str, Any],
    verified: bool,
) -> None:
    con.execute(
        """
        INSERT INTO control_store_initialization
          (singleton, state, mode, source_fingerprint, source_timezone, verified_at, details_json)
        VALUES (TRUE, ?, ?, ?, ?, CASE WHEN ? THEN now() ELSE NULL END, ?)
        ON CONFLICT (singleton) DO UPDATE SET
          state = excluded.state, mode = excluded.mode,
          source_fingerprint = excluded.source_fingerprint,
          source_timezone = excluded.source_timezone,
          verified_at = excluded.verified_at, details_json = excluded.details_json,
          updated_at = now()
        """,
        [
            state,
            mode,
            fingerprint,
            source_timezone,
            verified,
            json.dumps(details, sort_keys=True, separators=(",", ":")),
        ],
    )


def control_store_status(control_path: Path) -> dict[str, Any]:
    """Return operator state without treating schema existence as readiness."""
    if not is_postgres_control_store(control_path):
        return {"backend": "duckdb", "state": "legacy-ready", "ready": True}
    with control_plane_connection(control_path) as con:
        row = con.execute("""
            SELECT state, mode, source_fingerprint, source_timezone, verified_at, details_json
              FROM control_store_initialization WHERE singleton = TRUE
            """).fetchone()
    if row is None:
        return {"backend": "postgres", "state": "uninitialized", "ready": False}
    try:
        details = json.loads(row[5] or "{}")
    except (TypeError, ValueError):
        details = {}
    return {
        "backend": "postgres",
        "state": str(row[0]),
        "mode": str(row[1]),
        "source_fingerprint": row[2],
        "source_timezone": row[3],
        "verified_at": row[4],
        "details": details,
        "ready": row[0] == "ready",
        "recovery": "forward-recovery-only-after-postgresql-writes",
    }


def control_store_ready(control_path: Path) -> bool:
    return bool(control_store_status(Path(control_path)).get("ready"))


def require_control_store_ready(control_path: Path) -> None:
    status = control_store_status(Path(control_path))
    if not status["ready"]:
        raise RuntimeError(
            "PostgreSQL control store is not initialized and verified; "
            "run control-store init or control-store import/verify offline"
        )


def initialize_empty_control_store(control_path: Path) -> dict[str, Any]:
    """Explicitly approve a new empty target; bootstrap alone never does this."""
    if not is_postgres_control_store(control_path):
        raise ValueError(
            "empty initialization requires an explicit PostgreSQL control store"
        )
    with control_plane_connection(control_path) as con:
        con.execute("BEGIN")
        try:
            _lock_import_admission(con)
            current = con.execute("""
                SELECT state, mode FROM control_store_initialization
                 WHERE singleton = TRUE
                """).fetchone()
            if current is not None and current[0] == "ready":
                # Idempotent approval must preserve a verified import's
                # fingerprint and mode rather than rewriting it as empty.
                con.execute("COMMIT")
            else:
                if current is not None and (
                    current[0] == "initializing" or current[1] == "import"
                ):
                    raise RuntimeError(
                        "refusing empty initialization while an import owns this target"
                    )
                populated = {
                    table: int(
                        con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    )
                    for table in _ALL_TABLES
                }
                if any(populated.values()):
                    raise RuntimeError(
                        "refusing empty initialization for a populated target"
                    )
                _mark_state(
                    con,
                    state="ready",
                    mode="empty",
                    fingerprint=None,
                    source_timezone=None,
                    details={
                        "rows": populated,
                        "recovery": "forward-recovery-only-after-writes",
                    },
                    verified=True,
                )
                con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return control_store_status(Path(control_path))


def _logical_target_rows(con: object, table: str) -> list[dict[str, Any]]:
    if table == "harness_events":
        result = con.execute("""
            SELECT e.*, COALESCE(p.payload_json, e.payload_json) AS payload_json
              FROM harness_events e
              LEFT JOIN harness_event_payloads p ON p.event_id = e.event_id
            """)
    else:
        result = con.execute(f"SELECT * FROM {table}")
    columns = [item[0] for item in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def _iter_logical_target_events(con: object, *, batch_size: int = 500):
    query = """
        SELECT e.*, COALESCE(p.payload_json, e.payload_json) AS payload_json
          FROM harness_events e
          LEFT JOIN harness_event_payloads p ON p.event_id = e.event_id
         ORDER BY e.event_id COLLATE "C"
    """
    if getattr(con, "dialect", None) == "postgres":
        # Psycopg's ordinary cursor buffers its complete result client-side.
        # A named cursor instead streams rows from PostgreSQL while the caller
        # holds the explicit verification transaction below.
        raw = con._connection
        cursor = raw.cursor(name=f"drover_verify_{uuid4().hex}")
        try:
            cursor.execute(query)
            columns = [item.name for item in cursor.description]
            while rows := cursor.fetchmany(max(1, int(batch_size))):
                yield [dict(zip(columns, row)) for row in rows]
        finally:
            cursor.close()
        return
    result = con.execute(query)
    columns = [item[0] for item in result.description]
    while rows := result.fetchmany(max(1, int(batch_size))):
        yield [dict(zip(columns, row)) for row in rows]


def _table_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(rows), "hashes": sorted(_row_hash(row) for row in rows)}


def _stream_summary(batches: Any, *, columns: set[str] | None = None) -> dict[str, Any]:
    """Order-preserving canonical digest with O(batch-size) memory."""
    digest = hashlib.sha256()
    count = 0
    for batch in batches:
        for row in batch:
            projected = (
                {column: row.get(column) for column in columns}
                if columns is not None
                else row
            )
            digest.update(_row_hash(projected).encode("ascii"))
            count += 1
    return {"count": count, "hash": digest.hexdigest()}


def _project_columns(
    rows: list[dict[str, Any]], columns: set[str]
) -> list[dict[str, Any]]:
    """Compare the source's persisted shape, ignoring target-only nullable columns."""
    return [{column: row.get(column) for column in columns} for row in rows]


def _verify_rows(
    con: object,
    *,
    source_rows: dict[str, list[dict[str, Any]]],
    credential_rows: dict[str, list[dict[str, Any]]],
    source_snapshot: Path,
    source_zone: ZoneInfo,
) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    event_columns = set(_source_columns(source_snapshot, "harness_events"))
    expected_events = _stream_summary(
        _iter_source_rows(source_snapshot, "harness_events", source_zone),
    )
    actual_events = _stream_summary(
        _iter_logical_target_events(con), columns=event_columns
    )
    tables["harness_events"] = {
        "expected": expected_events["count"],
        "actual": actual_events["count"],
        "match": expected_events == actual_events,
    }
    for table in CONTROL_PLANE_TABLES:
        if table == "harness_events":
            continue
        expected = _table_summary(source_rows[table])
        source_columns = set().union(*(row.keys() for row in source_rows[table]))
        actual = _table_summary(
            _project_columns(_logical_target_rows(con, table), source_columns)
        )
        tables[table] = {
            "expected": expected["count"],
            "actual": actual["count"],
            "match": expected == actual,
        }
    for table in _EXTRA_TABLES:
        expected = _table_summary(credential_rows[table])
        source_columns = set().union(*(row.keys() for row in credential_rows[table]))
        actual = _table_summary(
            _project_columns(_logical_target_rows(con, table), source_columns)
        )
        tables[table] = {
            "expected": expected["count"],
            "actual": actual["count"],
            "match": expected == actual,
        }
    relationships = {
        "events_missing_session": int(con.execute("""
                SELECT count(*) FROM harness_events e
                 LEFT JOIN harness_sessions s ON s.session_id = e.session_id
                 WHERE s.session_id IS NULL
                """).fetchone()[0]),
        "sessions_missing_host": int(con.execute("""
                SELECT count(*) FROM harness_sessions s
                 LEFT JOIN harness_hosts h ON h.host_id = s.host_id
                 WHERE h.host_id IS NULL
                """).fetchone()[0]),
    }
    ok = all(item["match"] for item in tables.values()) and not any(
        relationships.values()
    )
    return {"ok": ok, "tables": tables, "relationships": relationships}


def _rebuild_previews(con: object) -> None:
    con.execute("DELETE FROM harness_session_previews")
    con.execute("""
        INSERT INTO harness_session_previews
          (session_id, event_id, content_preview, event_type, event_priority, seq, event_created_at)
        SELECT DISTINCT ON (session_id)
               session_id, event_id, COALESCE(content_preview, ''), event_type,
               CASE event_type WHEN 'user_input' THEN 0 WHEN 'terminal.input' THEN 1 ELSE 2 END,
               seq, created_at
          FROM harness_events
         WHERE event_type IN ('user_input', 'assistant_output', 'terminal.input')
         ORDER BY session_id,
                  CASE event_type WHEN 'user_input' THEN 0 WHEN 'terminal.input' THEN 1 ELSE 2 END,
                  COALESCE(seq, 0) DESC, created_at DESC, event_id DESC
        """)


def _lock_import_admission(con: object) -> None:
    """Serialize all snapshot imports for one target schema, not one source."""
    con.execute(
        "SELECT pg_advisory_xact_lock(hashtext(?))",
        ["drover-control-import-admission"],
    )


def _mark_import_failed_if_owned(
    con: object,
    *,
    fingerprint: str,
    source_timezone: str,
    error: Exception,
) -> None:
    """Only the import that left an initializing marker may mark it failed."""
    con.execute("BEGIN")
    try:
        _lock_import_admission(con)
        current = con.execute("""
            SELECT state, source_fingerprint FROM control_store_initialization
             WHERE singleton = TRUE FOR UPDATE
            """).fetchone()
        if (
            current is not None
            and current[0] == "initializing"
            and current[1] == fingerprint
        ):
            _mark_state(
                con,
                state="failed",
                mode="import",
                fingerprint=fingerprint,
                source_timezone=source_timezone,
                details={"error": type(error).__name__},
                verified=False,
            )
            con.execute("COMMIT")
            return
        con.execute("ROLLBACK")
    except Exception:
        con.execute("ROLLBACK")
        raise


def import_legacy_snapshot(
    control_path: Path,
    *,
    source_snapshot: Path,
    source_timezone: str,
    credential_document: Path | None = None,
) -> dict[str, Any]:
    """Import one read-only legacy snapshot and mark ready only after verification."""
    control_path = Path(control_path)
    source_snapshot = Path(source_snapshot)
    credential_document = Path(credential_document) if credential_document else None
    if not is_postgres_control_store(control_path):
        raise ValueError(
            "offline import target must be an explicit PostgreSQL control store"
        )
    zone = _zone(source_timezone)
    source_rows = _source_table_rows(source_snapshot, zone)
    credential_rows = _credential_rows(credential_document, zone)
    fingerprint = _source_fingerprint(source_snapshot, credential_document)
    with control_plane_connection(control_path) as con:
        owns_initialization = False
        try:
            # Commit the admission marker while holding the target-wide lock.
            # A waiting different snapshot then sees `initializing` instead of
            # an apparently empty target, even if this process subsequently
            # dies between transactions.
            con.execute("BEGIN")
            _lock_import_admission(con)
            current = con.execute(
                "SELECT state FROM control_store_initialization WHERE singleton = TRUE"
            ).fetchone()
            if current is not None and current[0] == "ready":
                raise RuntimeError(
                    "target is already ready; use forward recovery after PostgreSQL writes"
                )
            if current is not None and current[0] == "initializing":
                raise RuntimeError(
                    "another snapshot import is already initializing this target"
                )
            existing = sum(
                int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                for table in _ALL_TABLES
            )
            if existing:
                raise RuntimeError("refusing import into a populated unready target")
            _validate_source_inventory(
                con,
                source_snapshot=source_snapshot,
                credential_rows=credential_rows,
            )
            _mark_state(
                con,
                state="initializing",
                mode="import",
                fingerprint=fingerprint,
                source_timezone=source_timezone,
                details={
                    "source_tables": {
                        **{key: len(value) for key, value in source_rows.items()},
                        "harness_events": _stream_summary(
                            _iter_source_rows(source_snapshot, "harness_events", zone)
                        )["count"],
                    }
                },
                verified=False,
            )
            con.execute("COMMIT")
            owns_initialization = True

            con.execute("BEGIN")
            _lock_import_admission(con)
            for table in CONTROL_PLANE_TABLES:
                if table == "harness_events":
                    for rows in _iter_source_rows(source_snapshot, table, zone):
                        metadata = []
                        payloads = []
                        for row in rows:
                            payload = row.get("payload_json")
                            if payload is None:
                                payload = "{}"
                            payload = str(payload)
                            metadata.append({**row, "payload_json": None})
                            payloads.append(
                                {
                                    "event_id": row["event_id"],
                                    "payload_json": payload,
                                    "payload_sha256": payload_sha256(payload),
                                }
                            )
                        _insert_rows(con, table, metadata)
                        _insert_rows(con, "harness_event_payloads", payloads)
                        _insert_rows(
                            con,
                            "control_outbox_events",
                            [
                                {"event_id": row["event_id"], "state": "pending"}
                                for row in rows
                            ],
                        )
                else:
                    _insert_rows(con, table, source_rows[table])
            for table in _EXTRA_TABLES:
                _insert_rows(con, table, credential_rows[table])
            _rebuild_previews(con)
            verification = _verify_rows(
                con,
                source_rows=source_rows,
                credential_rows=credential_rows,
                source_snapshot=source_snapshot,
                source_zone=zone,
            )
            if not verification["ok"]:
                raise RuntimeError("import verification failed before readiness marker")
            _mark_state(
                con,
                state="ready",
                mode="import",
                fingerprint=fingerprint,
                source_timezone=source_timezone,
                details={
                    **verification,
                    "recovery": "forward-recovery-only-after-writes",
                },
                verified=True,
            )
            con.execute("COMMIT")
        except Exception as exc:
            con.execute("ROLLBACK")
            if owns_initialization:
                _mark_import_failed_if_owned(
                    con,
                    fingerprint=fingerprint,
                    source_timezone=source_timezone,
                    error=exc,
                )
            raise
    return control_store_status(control_path)


def verify_legacy_import(
    control_path: Path,
    *,
    source_snapshot: Path,
    source_timezone: str,
    credential_document: Path | None = None,
) -> dict[str, Any]:
    """Recompute counts/hashes against the explicit source without mutating either side."""
    control_path = Path(control_path)
    if not is_postgres_control_store(control_path):
        raise ValueError(
            "verification target must be an explicit PostgreSQL control store"
        )
    zone = _zone(source_timezone)
    source_snapshot = Path(source_snapshot)
    source_rows = _source_table_rows(source_snapshot, zone)
    credential_rows = _credential_rows(
        Path(credential_document) if credential_document else None, zone
    )
    with control_plane_connection(control_path) as con:
        con.execute("BEGIN")
        try:
            _validate_source_inventory(
                con,
                source_snapshot=source_snapshot,
                credential_rows=credential_rows,
            )
            report = _verify_rows(
                con,
                source_rows=source_rows,
                credential_rows=credential_rows,
                source_snapshot=source_snapshot,
                source_zone=zone,
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    report["status"] = control_store_status(control_path)
    return report
