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


def with_freshness(value: dict | None, *, empty_envelope: bool = False) -> dict | None:
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
        own = None
        for key, basis in (
            ("generated_at", "summary_generated_at"),
            ("freshness_ts", "brief_generated_at"),
            ("latest_ingested_at", "latest_ingested_at"),
            ("timestamp", "event_time"),
            ("source_timestamp", "source_timestamp"),
            ("updated_at", "control_state_updated_at"),
            ("last_event_at", "event_time"),
        ):
            instant = _instant(item.get(key))
            if instant is not None:
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
            item["store"] = "hub"
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
