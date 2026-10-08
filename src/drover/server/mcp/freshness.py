"""Provenance of returned MCP data; never substitute retrieval time for data."""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from typing import Any


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return (
        stamp.replace(tzinfo=timezone.utc)
        if stamp.tzinfo is None
        else stamp.astimezone(timezone.utc)
    )


def store_identity(path) -> dict:
    """Only a registered central PostgreSQL store can claim hub authority."""
    from drover.server.control_store import is_postgres_control_store

    authoritative = is_postgres_control_store(path)
    return {
        "store": "hub" if authoritative else "local",
        "store_authoritative": authoritative,
    }


def store_watermark(path):
    """A derived-store observation for empty recall, without scanning history."""
    from drover.server.db import control_plane_connection
    from drover.server.control_store import is_postgres_control_store

    if not is_postgres_control_store(path):
        return None
    try:
        with control_plane_connection(path, timeout=0.5) as con:
            row = con.execute("""
                SELECT MAX(stamp) FROM (
                  SELECT MAX(summary_generated_at) AS stamp FROM session_memory
                  UNION ALL SELECT MAX(recap_generated_at) FROM session_memory
                  UNION ALL SELECT MAX(generated_at) FROM project_briefs
                ) observed
            """).fetchone()
        return (row[0], "derived_store_generated_at") if row and row[0] else None
    except Exception:
        # Provenance must never turn an otherwise valid read into a failure.
        return None


def with_freshness(
    value: dict | None,
    *,
    empty_envelope: bool = False,
    path=None,
) -> dict | None:
    """Stamp the envelope and each data item with its own observed watermark.

    Envelope watermarks summarize returned data, not unrelated hub activity.
    Producer host is used on individual records when present; the envelope host
    identifies the hub process answering the request. Null is explicitly unknown.
    """
    if value is None:
        if not empty_envelope:
            return None
        value = {"status": "unavailable"}
    hub_host = socket.gethostname()
    identity = (
        store_identity(path)
        if path is not None
        else {
            "store": value.get("store", "hub"),
            "store_authoritative": value.get("store_authoritative", True),
        }
    )

    def walk(item, *, root=False):
        if isinstance(item, list):
            stamps = [walk(child) for child in item]
            return max((s for s in stamps if s), default=None, key=lambda s: s[0])
        if not isinstance(item, dict):
            return None
        stamps = []
        for key, child in list(item.items()):
            if key not in {"data_watermark", "query", "limits", "archive"}:
                stamp = walk(child)
                if stamp:
                    stamps.append(stamp)
        previous = item.get("data_watermark") or {}
        previous_time = _instant(previous.get("timestamp"))
        own = (
            (previous_time, previous.get("basis", "source_timestamp"))
            if previous_time
            else None
        )
        for key, basis in (
            ("generated_at", "summary_generated_at"),
            ("freshness_ts", "brief_generated_at"),
            ("latest_ingested_at", "latest_ingested_at"),
            ("timestamp", "event_time"),
            ("source_timestamp", "source_timestamp"),
            ("summary_generated_at", "summary_generated_at"),
            ("recap_generated_at", "recap_generated_at"),
            ("last_touched_at", "context_last_activity_at"),
            ("last_activity_at", "last_activity_at"),
            ("updated_at", "control_state_updated_at"),
            ("last_event_at", "event_time"),
            ("ended_at", "session_ended_at"),
            ("started_at", "session_started_at"),
        ):
            instant = _instant(item.get(key))
            if instant is not None and own is None:
                own = (instant, basis)
                break
        if own:
            stamps.append(own)
        stamp = own or max(stamps, default=None, key=lambda s: s[0])
        is_record = bool(
            {
                "session_id",
                "harness_id",
                "source_type",
                "brief_md",
                "summary_md",
                "context_id",
                "content",
                "recap_text",
                "project_key",
            }
            & item.keys()
        )
        if root or is_record:
            item.update(identity)
            if root and stamp is None and path is not None:
                observed = store_watermark(path)
                if observed:
                    stamp = (_instant(observed[0]), observed[1])
            item["host"] = (
                hub_host
                if root
                else item.get("host_id")
                or item.get("agent_id")
                or item.get("source_agent")
                or hub_host
            )
            item["data_watermark"] = {
                "timestamp": stamp[0].isoformat() if stamp else None,
                "basis": stamp[1] if stamp else "unknown",
            }
        return stamp

    walk(value, root=True)
    return value
