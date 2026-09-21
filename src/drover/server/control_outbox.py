"""Atomic PostgreSQL harness-event payload, preview, and outbox primitives.

The registry owns the event transaction.  This module deliberately receives an
already-open connection so a caller cannot commit an event without committing
its payload, cheap serving projection, and export intent in the same unit.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

PUBLISHED_BATCHES_DIR = "control_outbox_batches"
EXPORTED_HARNESS_EVENTS_RELATION = "harness_exported_events"


def _load_exclusive_rename() -> tuple[Any | None, int]:
    """Return the platform primitive that refuses to replace an existing entry."""
    try:
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            function = library.renameatx_np
            flag = 0x00000004  # RENAME_EXCL
        elif sys.platform.startswith("linux"):
            function = library.renameat2
            flag = 0x00000001  # RENAME_NOREPLACE
        else:
            return None, 0
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        return function, flag
    except (AttributeError, OSError, TypeError, ValueError):
        return None, 0


_EXCLUSIVE_RENAME, _EXCLUSIVE_RENAME_FLAG = _load_exclusive_rename()


def is_postgres_connection(con: object) -> bool:
    return getattr(con, "dialect", None) == "postgres"


def canonical_payload(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))


def payload_sha256(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def preview_priority(event_type: str) -> int:
    """Match the legacy full-history preview ordering exactly."""
    return {"user_input": 0, "terminal.input": 1}.get(event_type, 2)


def record_event_side_effects(
    con: object,
    *,
    event_id: str,
    session_id: str,
    event_type: str,
    content_preview: str | None,
    payload_json: str,
    created_at: datetime,
    seq: int | None,
) -> None:
    """Store split payload, narrow preview, and pending export intent.

    This is intentionally a no-op for DuckDB.  The legacy store preserves its
    inline event envelope and has no PostgreSQL outbox to export.
    """
    if not is_postgres_connection(con):
        return
    con.execute(
        """
        INSERT INTO harness_event_payloads (event_id, payload_json, payload_sha256)
        VALUES (?, ?, ?)
        ON CONFLICT (event_id) DO NOTHING
        """,
        [event_id, payload_json, payload_sha256(payload_json)],
    )
    if event_type in {"user_input", "assistant_output", "terminal.input"}:
        # The `WHERE` clause is the materialized equivalent of the legacy
        # ORDER BY: lower event-type priority wins; ties use coalesced seq,
        # timestamp, then event id.  A late lower-ranked event cannot clobber
        # a preferred preview already selected from history.
        con.execute(
            """
            INSERT INTO harness_session_previews
              (session_id, event_id, content_preview, event_type, event_priority,
               seq, event_created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_id) DO UPDATE SET
              event_id = excluded.event_id,
              content_preview = excluded.content_preview,
              event_type = excluded.event_type,
              event_priority = excluded.event_priority,
              seq = excluded.seq,
              event_created_at = excluded.event_created_at,
              updated_at = now()
            WHERE excluded.event_priority < harness_session_previews.event_priority
               OR (
                    excluded.event_priority = harness_session_previews.event_priority
                AND (
                       COALESCE(excluded.seq, 0) > COALESCE(harness_session_previews.seq, 0)
                    OR (
                           COALESCE(excluded.seq, 0) = COALESCE(harness_session_previews.seq, 0)
                       AND (
                              excluded.event_created_at > harness_session_previews.event_created_at
                           OR (
                                  excluded.event_created_at = harness_session_previews.event_created_at
                              AND excluded.event_id > harness_session_previews.event_id
                              )
                           )
                       )
                    )
               )
            """,
            [
                session_id,
                event_id,
                content_preview or "",
                event_type,
                preview_priority(event_type),
                seq,
                created_at,
            ],
        )
    con.execute(
        """
        INSERT INTO control_outbox_events (event_id, state)
        VALUES (?, 'pending')
        ON CONFLICT (event_id) DO NOTHING
        """,
        [event_id],
    )


def event_payload_join(con: object, event_alias: str = "e") -> str:
    """Return a narrow left join that preserves v1 inline rows during upgrade."""
    if not is_postgres_connection(con):
        return ""
    return (
        f" LEFT JOIN harness_event_payloads p ON p.event_id = {event_alias}.event_id "
    )


def event_payload_expression(con: object, event_alias: str = "e") -> str:
    if not is_postgres_connection(con):
        return f"{event_alias}.payload_json"
    return f"COALESCE(p.payload_json, {event_alias}.payload_json)"


def event_archive_join(con: object, event_alias: str = "e") -> str:
    """Attach archive availability metadata without opening archived bytes."""
    if not is_postgres_connection(con):
        return ""
    return (
        f" LEFT JOIN harness_event_archives a ON a.event_id = {event_alias}.event_id "
    )


@dataclass(frozen=True)
class OutboxClaim:
    batch_id: str
    event_ids: tuple[str, ...]
    lease_owner: str
    lease_until: datetime


@dataclass(frozen=True)
class PublishedBatch:
    batch_id: str
    archive_path: str
    content_sha256: str
    member_count: int
    published_at: datetime


def _batch_id(event_ids: list[str]) -> str:
    return hashlib.sha256("\n".join(event_ids).encode("utf-8")).hexdigest()


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def claim_outbox_batch(
    con: object,
    *,
    owner: str,
    limit: int = 100,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> OutboxClaim | None:
    """Durably claim pending work, recovering one expired batch before new work.

    Reclaiming the existing batch membership first gives a crash/retry the same
    stable id.  New work is selected by durable commit order, never a max-id
    cursor, so a late transaction cannot be skipped.
    """
    if not is_postgres_connection(con):
        raise ValueError("the control outbox requires a PostgreSQL connection")
    if not owner.strip():
        raise ValueError("owner is required")
    stamp = _utc_now(now)
    lease_until = stamp + timedelta(seconds=max(1, int(lease_seconds)))
    con.execute("BEGIN")
    try:
        expired = con.execute(
            """
            SELECT batch_id FROM control_outbox_batches
             WHERE state = 'claimed' AND lease_until < ?
             ORDER BY created_at, batch_id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
            """,
            [stamp],
        ).fetchone()
        if expired is not None:
            batch_id = str(expired[0])
            rows = con.execute(
                """
                SELECT event_id FROM control_outbox_batch_events
                 WHERE batch_id = ? ORDER BY ordinal
                """,
                [batch_id],
            ).fetchall()
            con.execute(
                """
                UPDATE control_outbox_batches SET lease_owner = ?, lease_until = ?
                 WHERE batch_id = ?
                """,
                [owner, lease_until, batch_id],
            )
            con.execute(
                """
                UPDATE control_outbox_events SET lease_owner = ?, lease_until = ?
                 WHERE batch_id = ? AND state = 'claimed'
                """,
                [owner, lease_until, batch_id],
            )
            con.execute("COMMIT")
            return OutboxClaim(
                batch_id, tuple(str(row[0]) for row in rows), owner, lease_until
            )

        rows = con.execute(
            """
            SELECT event_id FROM control_outbox_events
             WHERE state = 'pending'
             ORDER BY committed_at, event_id
             FOR UPDATE SKIP LOCKED
             LIMIT ?
            """,
            [max(1, int(limit))],
        ).fetchall()
        event_ids = [str(row[0]) for row in rows]
        if not event_ids:
            con.execute("COMMIT")
            return None
        batch_id = _batch_id(event_ids)
        con.execute(
            """
            INSERT INTO control_outbox_batches
              (batch_id, state, lease_owner, lease_until, member_count)
            VALUES (?, 'claimed', ?, ?, ?)
            ON CONFLICT (batch_id) DO UPDATE SET
              state = 'claimed', lease_owner = excluded.lease_owner,
              lease_until = excluded.lease_until
            """,
            [batch_id, owner, lease_until, len(event_ids)],
        )
        for ordinal, event_id in enumerate(event_ids):
            con.execute(
                """
                INSERT INTO control_outbox_batch_events (batch_id, event_id, ordinal)
                VALUES (?, ?, ?)
                ON CONFLICT (batch_id, event_id) DO NOTHING
                """,
                [batch_id, event_id, ordinal],
            )
        con.execute(
            """
            UPDATE control_outbox_events
               SET state = 'claimed', batch_id = ?, lease_owner = ?, lease_until = ?
             WHERE event_id IN (SELECT event_id FROM control_outbox_batch_events WHERE batch_id = ?)
               AND state = 'pending'
            """,
            [batch_id, owner, lease_until, batch_id],
        )
        con.execute("COMMIT")
        return OutboxClaim(batch_id, tuple(event_ids), owner, lease_until)
    except Exception:
        con.execute("ROLLBACK")
        raise


def _claim_rows(con: object, claim: OutboxClaim) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT m.ordinal AS outbox_ordinal,
               e.event_id, e.session_id, e.event_type, e.normalized_type,
               e.normalized_source, e.content_preview, e.created_at, e.seq,
               e.dedup_key, COALESCE(p.payload_json, e.payload_json) AS payload_json,
               p.payload_sha256
          FROM control_outbox_batch_events m
          JOIN harness_events e ON e.event_id = m.event_id
          LEFT JOIN harness_event_payloads p ON p.event_id = e.event_id
         WHERE m.batch_id = ?
         ORDER BY m.ordinal
        """,
        [claim.batch_id],
    )
    cols = [description[0] for description in rows.description]
    return [dict(zip(cols, row, strict=True)) for row in rows.fetchall()]


def _batch_path(parquet_dir: Path, batch_id: str) -> Path:
    return Path(parquet_dir) / PUBLISHED_BATCHES_DIR / f"{batch_id}.parquet"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_claim_for_publication(con: object, claim: OutboxClaim) -> None:
    """Confirm durable lease and ordinal membership before touching its final path."""
    batch = con.execute(
        """
        SELECT state, lease_owner, member_count
          FROM control_outbox_batches WHERE batch_id = ?
        """,
        [claim.batch_id],
    ).fetchone()
    if batch is None or batch[0] not in {"claimed", "published", "acknowledged"}:
        raise RuntimeError("outbox claim is no longer publishable")
    if batch[0] == "claimed" and batch[1] != claim.lease_owner:
        raise RuntimeError("outbox claim lease owner changed before publication")
    membership = con.execute(
        """
        SELECT event_id FROM control_outbox_batch_events
         WHERE batch_id = ? ORDER BY ordinal
        """,
        [claim.batch_id],
    ).fetchall()
    event_ids = tuple(str(row[0]) for row in membership)
    if int(batch[2]) != len(claim.event_ids) or event_ids != claim.event_ids:
        raise RuntimeError("outbox claim membership changed before publication")


def _validate_existing_batch(path: Path, expected: pa.Table) -> None:
    """Accept a crash-retry file only when every claimed logical fact agrees."""
    descriptor = _open_regular_file(path)
    os.close(descriptor)
    try:
        existing = pq.ParquetFile(path).read()
    except Exception as exc:
        raise RuntimeError("existing outbox batch is unreadable") from exc
    if (
        existing.schema.remove_metadata() != expected.schema.remove_metadata()
        or existing.to_pylist() != expected.to_pylist()
    ):
        raise RuntimeError("existing outbox batch does not match claimed membership")


def _open_regular_file(path: Path) -> int:
    """Open one local regular file without accepting a symlinked batch."""
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(errno.EINVAL, "outbox batch is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            os.close(descriptor)
            raise OSError(errno.EINVAL, "outbox batch changed while opening")
        return descriptor
    except OSError as exc:
        raise RuntimeError("existing outbox batch is not a regular local file") from exc


def _sync_regular_file(path: Path) -> None:
    """Flush a validated regular file before publishing its directory entry."""
    descriptor = _open_regular_file(path)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    return os.open(path, flags)


def _ensure_publication_directory(path: Path) -> None:
    """Create the one private batch directory and persist a new parent entry."""
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        _ensure_publication_directory(path.parent)
        try:
            path.mkdir()
        except FileExistsError:
            existing = os.lstat(path)
            if not stat.S_ISDIR(existing.st_mode):
                raise RuntimeError("outbox publication path is not a directory")
        else:
            parent_descriptor = _open_directory(path.parent)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        return
    if not stat.S_ISDIR(existing.st_mode):
        raise RuntimeError("outbox publication path is not a directory")


def _entry_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _rename_noreplace_at(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
) -> None:
    """Atomically link the attempt to its final name only if final is absent."""
    function = _EXCLUSIVE_RENAME
    flag = _EXCLUSIVE_RENAME_FLAG
    if function is None or flag == 0:
        raise OSError(errno.ENOTSUP, "exclusive outbox publication unsupported")
    ctypes.set_errno(0)
    result = function(
        source_descriptor,
        os.fsencode(source_name),
        destination_descriptor,
        os.fsencode(destination_name),
        flag,
    )
    if result == 0:
        return
    raise OSError(
        ctypes.get_errno() or errno.EIO,
        "exclusive outbox publication failed",
    )


def _durably_validate_existing_batch(
    path: Path, expected: pa.Table, directory_descriptor: int
) -> str:
    """Recover an already-written final only after logical and sync barriers."""
    _validate_existing_batch(path, expected)
    _sync_regular_file(path)
    os.fsync(directory_descriptor)
    return _file_sha256(path)


def _publish_immutable_batch(table: pa.Table, path: Path) -> str:
    """Return a durable immutable final file, without making an SQL receipt."""
    _ensure_publication_directory(path.parent)
    directory_descriptor = _open_directory(path.parent)
    temporary_path: Path | None = None
    renamed = False
    try:
        if _entry_exists(path):
            return _durably_validate_existing_batch(path, table, directory_descriptor)
        if _EXCLUSIVE_RENAME is None or _EXCLUSIVE_RENAME_FLAG == 0:
            raise OSError(errno.ENOTSUP, "exclusive outbox publication unsupported")
        temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        pq.write_table(table, temporary_path, compression="zstd")
        _sync_regular_file(temporary_path)
        try:
            _rename_noreplace_at(
                directory_descriptor,
                temporary_path.name,
                directory_descriptor,
                path.name,
            )
        except FileExistsError:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            temporary_path = None
            return _durably_validate_existing_batch(path, table, directory_descriptor)
        renamed = True
        temporary_path = None
        _validate_existing_batch(path, table)
        _sync_regular_file(path)
        os.fsync(directory_descriptor)
        return _file_sha256(path)
    finally:
        if temporary_path is not None and not renamed:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        os.close(directory_descriptor)


def _record_published_batch(
    con: object,
    claim: OutboxClaim,
    *,
    path: Path,
    content_hash: str,
    member_count: int,
    stamp: datetime,
) -> PublishedBatch:
    """Make an already durable immutable batch visible in the SQL manifest."""
    con.execute("BEGIN")
    try:
        result = con.execute(
            """
            UPDATE control_outbox_batches
               SET state = 'published', content_sha256 = ?, archive_path = ?, published_at = ?
             WHERE batch_id = ? AND state = 'claimed' AND lease_owner = ?
            RETURNING member_count
            """,
            [content_hash, str(path), stamp, claim.batch_id, claim.lease_owner],
        ).fetchone()
        if result is None:
            existing = con.execute(
                """
                SELECT state, content_sha256, archive_path, member_count, published_at
                  FROM control_outbox_batches WHERE batch_id = ?
                """,
                [claim.batch_id],
            ).fetchone()
            if existing is None or existing[0] not in {"published", "acknowledged"}:
                raise RuntimeError("outbox claim is no longer publishable")
            if existing[1] != content_hash or Path(str(existing[2])) != path:
                raise RuntimeError(
                    "published outbox batch receipt does not match immutable file"
                )
            con.execute("COMMIT")
            return PublishedBatch(
                claim.batch_id,
                str(existing[2]),
                str(existing[1]),
                int(existing[3]),
                existing[4],
            )
        con.execute(
            """
            UPDATE control_outbox_events
               SET state = 'published', published_at = ?
             WHERE batch_id = ? AND state = 'claimed'
            """,
            [stamp, claim.batch_id],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return PublishedBatch(claim.batch_id, str(path), content_hash, member_count, stamp)


def publish_outbox_batch(
    con: object,
    claim: OutboxClaim,
    *,
    parquet_dir: Path,
    now: datetime | None = None,
) -> PublishedBatch:
    """Publish one claim to a fixed immutable path, then make it visible in SQL."""
    if not is_postgres_connection(con):
        raise ValueError("the control outbox requires a PostgreSQL connection")
    _validate_claim_for_publication(con, claim)
    rows = _claim_rows(con, claim)
    if tuple(str(row["event_id"]) for row in rows) != claim.event_ids:
        raise RuntimeError("outbox claim membership changed before publication")
    for row in rows:
        payload = row.get("payload_json")
        if not isinstance(payload, str):
            raise RuntimeError(
                f"outbox event {row['event_id']} has no retained payload"
            )
        expected = str(row.get("payload_sha256") or "")
        actual = payload_sha256(payload)
        if expected and expected != actual:
            raise RuntimeError(f"outbox event {row['event_id']} payload hash mismatch")
        row["payload_sha256"] = actual
    table = pa.Table.from_pylist(rows)
    path = _batch_path(Path(parquet_dir), claim.batch_id)
    # A crash after final publication but before the SQL receipt is a retry,
    # never a second logical batch. The helper validates schema, ordinal
    # membership, and every raw payload before it can expose that final path.
    content_hash = _publish_immutable_batch(table, path)
    stamp = _utc_now(now)
    return _record_published_batch(
        con,
        claim,
        path=path,
        content_hash=content_hash,
        member_count=len(rows),
        stamp=stamp,
    )


def acknowledge_outbox_batch(
    con: object, batch_id: str, *, now: datetime | None = None
) -> bool:
    """Acknowledge only a SQL-visible immutable batch."""
    if not is_postgres_connection(con):
        raise ValueError("the control outbox requires a PostgreSQL connection")
    stamp = _utc_now(now)
    con.execute("BEGIN")
    try:
        row = con.execute(
            """
            UPDATE control_outbox_batches SET state = 'acknowledged', acknowledged_at = ?
             WHERE batch_id = ? AND state = 'published'
            RETURNING batch_id
            """,
            [stamp, batch_id],
        ).fetchone()
        if row is None:
            existing = con.execute(
                "SELECT state FROM control_outbox_batches WHERE batch_id = ?",
                [batch_id],
            ).fetchone()
            con.execute("COMMIT")
            return existing is not None and existing[0] == "acknowledged"
        con.execute(
            """
            UPDATE control_outbox_events
               SET state = 'acknowledged', acknowledged_at = ?
             WHERE batch_id = ? AND state = 'published'
            """,
            [stamp, batch_id],
        )
        con.execute("COMMIT")
        return True
    except Exception:
        con.execute("ROLLBACK")
        raise


def published_batches(con: object) -> list[PublishedBatch]:
    """The authoritative analytical read contract: SQL manifest, never a glob."""
    if not is_postgres_connection(con):
        return []
    rows = con.execute("""
        SELECT batch_id, archive_path, content_sha256, member_count, published_at
          FROM control_outbox_batches
         WHERE state IN ('published', 'acknowledged')
           AND archive_path IS NOT NULL AND content_sha256 IS NOT NULL
         ORDER BY published_at, batch_id
        """).fetchall()
    return [
        PublishedBatch(str(row[0]), str(row[1]), str(row[2]), int(row[3]), row[4])
        for row in rows
    ]


def register_published_harness_events_relation(
    analytical_con: object, control_con: object
) -> str:
    """Register the worker's distinct harness-event relation from SQL manifest rows.

    This intentionally never globs a directory and never reuses
    ``agent_events``: the source grain is central harness delivery, whereas
    collector events may represent the same activity independently.
    """
    paths: list[str] = []
    for batch in published_batches(control_con):
        path = Path(batch.archive_path)
        if not path.is_file() or _file_sha256(path) != batch.content_sha256:
            raise RuntimeError(
                f"published outbox batch {batch.batch_id} failed verification"
            )
        paths.append(str(path))
    if not paths:
        analytical_con.execute(
            f"CREATE OR REPLACE VIEW {EXPORTED_HARNESS_EVENTS_RELATION} AS "
            "SELECT CAST(NULL AS VARCHAR) AS event_id WHERE FALSE"
        )
        return EXPORTED_HARNESS_EVENTS_RELATION
    literals = ", ".join("'" + path.replace("'", "''") + "'" for path in paths)
    analytical_con.execute(
        f"CREATE OR REPLACE VIEW {EXPORTED_HARNESS_EVENTS_RELATION} AS "
        f"SELECT * FROM read_parquet([{literals}], union_by_name=true)"
    )
    return EXPORTED_HARNESS_EVENTS_RELATION


def outbox_status(con: object) -> dict[str, Any]:
    """Return bounded exporter freshness without suggesting a global cursor."""
    if not is_postgres_connection(con):
        return {
            "enabled": False,
            "pending": 0,
            "claimed": 0,
            "published_unacknowledged": 0,
            "oldest_pending_at": None,
            "oldest_outstanding_at": None,
        }
    row = con.execute("""
        SELECT count(*) FILTER (WHERE state = 'pending'),
               min(committed_at) FILTER (WHERE state = 'pending'),
               count(*) FILTER (WHERE state = 'claimed'),
               count(*) FILTER (WHERE state = 'published'),
               count(*) FILTER (WHERE state = 'acknowledged'),
               min(committed_at) FILTER (WHERE state IN ('pending', 'claimed', 'published'))
          FROM control_outbox_events
        """).fetchone()
    return {
        "enabled": True,
        "pending": int(row[0] or 0),
        "oldest_pending_at": row[1],
        "claimed": int(row[2] or 0),
        "published_unacknowledged": int(row[3] or 0),
        "acknowledged": int(row[4] or 0),
        "oldest_outstanding_at": row[5],
    }


def _payload_prune_candidates(
    con: object, *, limit: int, event_id: str | None = None
) -> list[dict[str, Any]]:
    """Detach hot-payload retention facts from a short PostgreSQL read."""
    filter_sql = "WHERE p.event_id = ?" if event_id is not None else ""
    params: list[Any] = [event_id] if event_id is not None else []
    params.append(max(1, int(limit)))
    rows = con.execute(
        f"""
        WITH session_progress AS (
          SELECT session_id, count(*) AS event_count, COALESCE(max(seq), 0) AS max_seq
            FROM harness_events GROUP BY session_id
        )
        SELECT p.event_id, p.payload_sha256,
               s.status, o.state AS outbox_state, o.batch_id, b.state AS batch_state,
               progress.event_count, progress.max_seq,
               usage.source_event_count, usage.source_seq,
               recap_job.status AS recap_status,
               recap_job.desired_source_seq AS recap_desired_source_seq,
               recap.source_seq AS recap_source_seq
          FROM harness_event_payloads p
          JOIN harness_events e ON e.event_id = p.event_id
          LEFT JOIN harness_sessions s ON s.session_id = e.session_id
          LEFT JOIN control_outbox_events o ON o.event_id = e.event_id
          LEFT JOIN control_outbox_batches b ON b.batch_id = o.batch_id
          LEFT JOIN session_progress progress ON progress.session_id = e.session_id
          LEFT JOIN session_usage_sources usage
            ON usage.session_id = e.session_id AND usage.source = 'harness_events'
          LEFT JOIN live_recap_jobs recap_job ON recap_job.session_id = e.session_id
          LEFT JOIN live_session_recaps recap ON recap.session_id = e.session_id
          {filter_sql}
         ORDER BY e.created_at, e.event_id
         LIMIT ?
        """,
        params,
    )
    columns = [item[0] for item in rows.description]
    return [dict(zip(columns, row)) for row in rows.fetchall()]


def _payload_prune_protection(row: dict[str, Any]) -> str | None:
    terminal = {"completed", "terminated", "errored", "failed"}
    if str(row.get("status") or "") not in terminal:
        return "active"
    if (
        row.get("outbox_state") != "acknowledged"
        or row.get("batch_id") is None
        or row.get("batch_state") != "acknowledged"
        or row.get("source_event_count") is None
        or int(row["source_event_count"]) < int(row.get("event_count") or 0)
        or int(row.get("source_seq") or 0) < int(row.get("max_seq") or 0)
        or row.get("recap_status") != "done"
        or int(row.get("recap_desired_source_seq") or 0) < int(row.get("max_seq") or 0)
        or int(row.get("recap_source_seq") or 0) < int(row.get("max_seq") or 0)
    ):
        return "dependency"
    return None


def prune_verified_payloads(
    control_path: Path,
    *,
    resolver: "ArchiveResolver | None",
    limit: int = 100,
    now: datetime | None = None,
) -> dict[str, int]:
    """Prune verified payloads without retaining a PostgreSQL slot during RPC.

    The caller supplies a configured control-store path, never an open
    connection. Candidate references are read and released before archive
    resolution. Each verified reference is then re-read in a fresh transaction
    before its immutable receipt and conditional payload deletion are written.
    """
    from drover.server.control_store import is_postgres_control_store
    from drover.server.db import control_plane_connection

    result = {
        "pruned": 0,
        "protected_active": 0,
        "protected_dependency": 0,
        "verification_failed": 0,
    }
    control_path = Path(control_path)
    if not is_postgres_control_store(control_path):
        return result
    with control_plane_connection(control_path) as con:
        candidates = _payload_prune_candidates(con, limit=limit)
    stamp = _utc_now(now)
    for candidate in candidates:
        protection = _payload_prune_protection(candidate)
        if protection == "active":
            result["protected_active"] += 1
            continue
        if protection == "dependency":
            result["protected_dependency"] += 1
            continue
        expected_hash = str(candidate.get("payload_sha256") or "")
        batch_id = candidate.get("batch_id")
        if not expected_hash or not isinstance(batch_id, str) or resolver is None:
            result["verification_failed"] += 1
            continue
        payload = resolver.resolve(
            event_id=str(candidate["event_id"]),
            batch_id=batch_id,
            payload_sha256=expected_hash,
        )
        if payload is None or payload_sha256(payload) != expected_hash:
            result["verification_failed"] += 1
            continue
        with control_plane_connection(control_path) as con:
            con.execute("BEGIN")
            try:
                current = _payload_prune_candidates(
                    con, limit=1, event_id=str(candidate["event_id"])
                )
                if (
                    len(current) != 1
                    or current[0].get("payload_sha256") != expected_hash
                    or current[0].get("batch_id") != batch_id
                    or _payload_prune_protection(current[0]) is not None
                ):
                    con.execute("ROLLBACK")
                    result["protected_dependency"] += 1
                    continue
                con.execute(
                    """
                    INSERT INTO harness_event_archives
                      (event_id, batch_id, payload_sha256, verified_at, payload_pruned_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (event_id) DO UPDATE SET
                      batch_id = excluded.batch_id, payload_sha256 = excluded.payload_sha256,
                      verified_at = excluded.verified_at, payload_pruned_at = excluded.payload_pruned_at
                    """,
                    [candidate["event_id"], batch_id, expected_hash, stamp, stamp],
                )
                deleted = con.execute(
                    """
                    DELETE FROM harness_event_payloads
                     WHERE event_id = ? AND payload_sha256 = ?
                    RETURNING event_id
                    """,
                    [candidate["event_id"], expected_hash],
                ).fetchone()
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        if deleted is not None:
            result["pruned"] += 1
    return result


class ArchiveResolver(Protocol):
    def resolve(
        self, *, event_id: str, batch_id: str, payload_sha256: str
    ) -> str | None: ...


@dataclass(frozen=True)
class PayloadLookup:
    state: str
    payload_json: str | None
    reason: str | None = None
    batch_id: str | None = None
    payload_sha256: str | None = None


class LocalVerifiedArchiveResolver:
    """Worker/combined-mode resolver for archived immutable payload batches."""

    def __init__(self, parquet_dir: Path, manifest_reader: Callable[[], set[str]]):
        self._parquet_dir = Path(parquet_dir)
        self._manifest_reader = manifest_reader

    def resolve(
        self, *, event_id: str, batch_id: str, payload_sha256: str
    ) -> str | None:
        if batch_id not in self._manifest_reader():
            return None
        path = _batch_path(self._parquet_dir, batch_id)
        if not path.exists():
            return None
        table = pq.ParquetFile(path).read(
            columns=["event_id", "payload_json", "payload_sha256"]
        )
        for row in table.to_pylist():
            if str(row.get("event_id")) != event_id:
                continue
            payload = row.get("payload_json")
            actual_hash = (
                hashlib.sha256(payload.encode("utf-8")).hexdigest()
                if isinstance(payload, str)
                else ""
            )
            if actual_hash != payload_sha256:
                return None
            return payload
        return None


def event_payload_reference(con: object, event_id: str) -> PayloadLookup:
    """Read hot bytes or a cold archive reference while a control session is held."""
    if is_postgres_connection(con):
        row = con.execute(
            """
            SELECT p.payload_json, a.batch_id, a.payload_sha256
              FROM harness_events e
              LEFT JOIN harness_event_payloads p ON p.event_id = e.event_id
              LEFT JOIN harness_event_archives a ON a.event_id = e.event_id
             WHERE e.event_id = ?
            """,
            [event_id],
        ).fetchone()
        if row is None:
            return PayloadLookup("unavailable", None, "event_not_found")
        if isinstance(row[0], str):
            return PayloadLookup("hot", row[0])
        if not row[1]:
            return PayloadLookup("unavailable", None, "payload_not_retained")
        return PayloadLookup(
            "archive", None, batch_id=str(row[1]), payload_sha256=str(row[2])
        )
    row = con.execute(
        "SELECT payload_json FROM harness_events WHERE event_id = ?", [event_id]
    ).fetchone()
    return (
        PayloadLookup("hot", str(row[0]))
        if row is not None and row[0] is not None
        else PayloadLookup("unavailable", None, "event_not_found")
    )


def resolve_event_payload_reference(
    reference: PayloadLookup, *, event_id: str, resolver: ArchiveResolver | None = None
) -> PayloadLookup:
    """Resolve a cold reference after its PostgreSQL reader has been released."""
    if reference.state != "archive":
        return reference
    if resolver is None:
        return PayloadLookup("unavailable", None, "archive_resolver_required")
    if not reference.batch_id or not reference.payload_sha256:
        return PayloadLookup("unavailable", None, "archive_reference_invalid")
    payload = resolver.resolve(
        event_id=event_id,
        batch_id=reference.batch_id,
        payload_sha256=reference.payload_sha256,
    )
    return (
        PayloadLookup("archive", payload)
        if isinstance(payload, str)
        and payload_sha256(payload) == reference.payload_sha256
        else PayloadLookup("unavailable", None, "archive_verification_failed")
    )


def lookup_event_payload(
    con: object, event_id: str, *, resolver: ArchiveResolver | None = None
) -> PayloadLookup:
    """Combined/worker helper; API readers use the split reference functions."""
    return resolve_event_payload_reference(
        event_payload_reference(con, event_id), event_id=event_id, resolver=resolver
    )
