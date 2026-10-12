"""Bounded, read-only import of OpenClaw's registered transcript archives."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from drover.collect.openclaw_sqlite import (
    _MAX_EVENT_BYTES,
    _DecoderUnavailable,
    _epoch_datetime,
    _load_zstd,
    _map_record,
)

_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_decompress = _load_zstd(_MAX_ARCHIVE_BYTES)


def _records(data: bytes):
    """Frame JSONL including retained pretty-printed JSON records."""
    text = data.decode("utf-8")
    decoder = json.JSONDecoder()
    offset = 0
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset == len(text):
            break
        start = offset
        record, offset = decoder.raw_decode(text, offset)
        if len(text[start:offset].encode("utf-8")) > _MAX_EVENT_BYTES:
            raise ValueError("archive record too large")
        if not isinstance(record, dict):
            raise ValueError("invalid archive record")
        yield record


def _bytes(reader, table, session_id, generation, meta, db_path):
    if table == "session_transcript_cold_archives" and meta["storage"] == "file":
        name = meta["archive_name"]
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-f0-9]{64}\.jsonl\.zst", name
        ):
            raise ValueError("invalid cold archive name")
        root = (db_path.parent.parent / "sessions" / "cold").resolve()
        path = root / name
        if path.resolve().parent != root:
            raise ValueError("cold archive leaves artifact directory")
        with path.open("rb") as stream:
            data = stream.read(_MAX_ARCHIVE_BYTES + 1)
    else:
        rows = reader.query(
            f"SELECT archive_blob FROM {table} WHERE session_id = ? AND generation = ? "
            "AND length(archive_blob) <= ?",
            (session_id, generation, _MAX_ARCHIVE_BYTES),
        )
        if not rows or rows[0][0] is None:
            raise ValueError("archive unavailable or too large")
        data = bytes(rows[0][0])
    if len(data) > _MAX_ARCHIVE_BYTES:
        raise ValueError("archive too large")
    if (
        table == "session_transcript_cold_archives"
        and len(data) != meta["archive_bytes"]
    ):
        raise ValueError("archive size mismatch")
    if hashlib.sha256(data).hexdigest() != meta["archive_sha256"]:
        raise ValueError("archive hash mismatch")
    encoding = meta.get("encoding", "zstd")
    if encoding == "zstd":
        if _decompress is None:
            raise _DecoderUnavailable()
        try:
            data = _decompress(data)
        except Exception as exc:
            raise ValueError("invalid compressed archive") from exc
    elif encoding != "identity":
        raise ValueError("unknown archive encoding")
    if len(data) > _MAX_ARCHIVE_BYTES:
        raise ValueError("decoded archive too large")
    return data


def collect_archives(
    source, agent_id, db_path: Path, reader, cursors, budget, events, diagnostics
):
    """Resume independently per retained generation, sharing the live event budget."""
    ok = True
    for table in ("session_transcript_archives", "session_transcript_cold_archives"):
        if table not in reader.tables:
            continue
        columns = tuple(c for c in reader.tables[table] if c != "archive_blob")
        after = ("", "")
        retained = set()
        while budget > 0:
            rows = reader.query(
                f"SELECT {', '.join(columns)} FROM {table} "
                "WHERE (session_id, generation) > (?, ?) ORDER BY session_id, generation LIMIT 1",
                after,
            )
            if not rows:
                for key in list(cursors):
                    try:
                        owner = json.loads(key)[0]
                    except (ValueError, TypeError, IndexError):
                        owner = table
                    if owner == table and key not in retained:
                        del cursors[key]
                break
            meta = dict(zip(columns, rows[0]))
            session_id, generation = meta["session_id"], meta["generation"]
            after = (session_id, generation)
            key = json.dumps([table, session_id, generation])
            retained.add(key)
            saved = cursors.get(key, {})
            if (
                not isinstance(saved, dict)
                or saved.get("sha256") != meta["archive_sha256"]
            ):
                saved = {}
            if saved.get("complete"):
                continue
            try:
                records = list(
                    _records(
                        _bytes(reader, table, session_id, generation, meta, db_path)
                    )
                )
                if not records:
                    raise ValueError("empty archive")
                cold = table == "session_transcript_cold_archives"
                head = records[0]
                if cold:
                    if (
                        head.get("kind") != "header"
                        or head.get("version") != 1
                        or head.get("sessionId") != session_id
                        or head.get("generation") != generation
                    ):
                        raise ValueError("cold archive identity mismatch")
                    transcript = []
                    for record in records[1:]:
                        if record.get("kind") == "event":
                            row = record["row"]
                            transcript.append(
                                (
                                    row["seq"],
                                    row["created_at"],
                                    json.loads(row["event_json"]),
                                )
                            )
                else:
                    if head.get("type") != "session" or head.get("id") != session_id:
                        raise ValueError("archive session identity mismatch")
                    transcript = [
                        (seq, meta["created_at"], record)
                        for seq, record in enumerate(records)
                    ]
                if not transcript or any(
                    not isinstance(record, dict)
                    or not isinstance(seq, int)
                    or _epoch_datetime(created_at) is None
                    for seq, created_at, record in transcript
                ):
                    raise ValueError("invalid transcript record")
                mapper, extra = source._session_mapper(
                    agent_id, reader, session_id, reader.app_version(), None, meta
                )
                offset = saved.get("offset", 0)
                if not isinstance(offset, int) or offset < 0:
                    offset = 0
                for index, (seq, created_at, record) in enumerate(transcript):
                    if index < offset:
                        if record.get("type") == "session":
                            mapper.feed(record)
                        continue
                    if budget <= 0:
                        break
                    event = _map_record(
                        mapper, record, agent_id, session_id, seq, created_at, extra
                    )
                    if event is None and record.get("type") != "session":
                        raise ValueError("unmappable archived transcript record")
                    if event is not None:
                        event.raw_data["openclaw_store"]["kind"] = "archive"
                        events.append(event)
                        budget -= 1
                    offset = index + 1
                cursors[key] = {
                    "sha256": meta["archive_sha256"],
                    "offset": offset,
                    "complete": offset >= len(transcript),
                }
            except _DecoderUnavailable:
                ok = False
                diagnostics.append(
                    f"agent {agent_id!r}: archive paused; zstd decoder unavailable"
                )
            except (OSError, ValueError, TypeError, KeyError) as exc:
                ok = False
                # Archive content, identifiers and filesystem paths stay out of diagnostics.
                diagnostics.append(
                    f"agent {agent_id!r}: archive unavailable or invalid ({type(exc).__name__}); retry next run"
                )
    return ok
