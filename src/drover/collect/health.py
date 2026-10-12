"""Collector progress metadata, separate from event content and event time."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

COLLECTOR_WARN_SECONDS = 600
COLLECTOR_CRITICAL_SECONDS = 3600


def stage_health(staging_dir: Path, source_id: str, payload: dict) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    final = staging_dir / f".collector-{source_id}.json"
    # Explicit allow-list: never ship cursor internals or transcript data.
    status = {
        key: payload.get(key)
        for key in (
            "last_successful_collect_at",
            "last_run_iso",
            "collect_successful",
            "caught_up",
        )
    }
    status["source_id"] = source_id
    tmp = final.with_suffix(".json.tmp")
    with tmp.open("w") as stream:
        json.dump(status, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, final)


def read_collector_health(incoming_dir: Path | None, *, now=None) -> list[dict]:
    """Read received heartbeats. Missing telemetry is unknown, never fresh."""
    if incoming_dir is None:
        return []
    now = now or datetime.now(timezone.utc)
    rows = []
    for path in sorted(Path(incoming_dir).glob("*/.collector-*.json")):
        try:
            if path.stat().st_size > 4096:
                raise ValueError("oversized status")
            status = json.loads(path.read_text())
            stamp = status.get("last_successful_collect_at")
            last = (
                datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None
            )
            if last and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            lag = max(0, (now - last).total_seconds()) if last else None
            state = (
                "unknown"
                if lag is None
                else (
                    "critical"
                    if lag >= COLLECTOR_CRITICAL_SECONDS
                    else (
                        "warn"
                        if lag >= COLLECTOR_WARN_SECONDS
                        or status.get("collect_successful") is False
                        or status.get("caught_up") is False
                        else "ok"
                    )
                )
            )
        except (OSError, ValueError, TypeError, AttributeError):
            stamp, lag, state = None, None, "unknown"
        rows.append(
            {
                "host_id": path.parent.name,
                "source_id": path.name[len(".collector-") : -len(".json")],
                "last_successful_collect_at": stamp,
                "collector_lag_seconds": lag,
                "status": state,
            }
        )
    return rows


def format_collector_health(rows: list[dict]) -> str:
    lines = ["collectors:"]
    for row in rows:
        lag = row["collector_lag_seconds"]
        lines.append(
            f"  {row['host_id']}/{row['source_id']}: {row['status']}; "
            f"last successful collect={row['last_successful_collect_at'] or 'unknown'}; "
            f"collector lag={f'{lag:.1f}s' if lag is not None else 'unknown'}"
        )
    if not rows:
        lines.append("  unknown (no collector health metadata received)")
    return "\n".join(lines)
