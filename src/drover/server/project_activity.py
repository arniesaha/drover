"""Project Activity: what happened on a project recently, and what is open.

Built only from Drover's own records (#473): harness launches and state,
agent-event day summaries, session summaries and ``session_usage``. The
session set is the cockpit's own session facts with spans excluded, so the
two surfaces always agree on which sessions exist. See
docs/design/2026-10-session-graph-and-activity.md.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import duckdb

from drover.server.cockpit.analytics import (
    AnalyticsFilters,
    materialized_session_facts,
)
from drover.server.session_graph import STUCK_REASONS, first_line, session_state

#: Hard caps on everything returned.
MAX_DAYS = 30
MAX_PROJECTS = 20
MAX_SESSIONS = 200
MAX_OPEN_ITEMS = 20
TEXT_CHARS = 300
#: Every interval in the window feeds active hours; this bounds that read.
_MAX_INTERVALS = 20_000


def _clip(text: Any, limit: int = TEXT_CHARS) -> str | None:
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _active_hours(intervals: list[tuple[datetime, datetime]]) -> float:
    """Wall-clock hours with at least one session active (overlaps count once)."""
    total = timedelta()
    end: datetime | None = None
    start: datetime | None = None
    for lo, hi in sorted(intervals):
        if end is None or lo > end:
            if end is not None and start is not None:
                total += end - start
            start, end = lo, hi
        elif hi > end:
            end = hi
    if end is not None and start is not None:
        total += end - start
    return round(total.total_seconds() / 3600.0, 2)


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any]) -> list[dict]:
    cursor = con.execute(sql, params)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _optional_rows(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any]
) -> list[dict]:
    """Decorations (state, summaries, recaps) never fail the whole answer."""
    try:
        return _rows(con, sql, params)
    except duckdb.Error:
        return []


def project_activity(
    con: duckdb.DuckDBPyConnection,
    *,
    project_key: str | None = None,
    days: int = 7,
    now: datetime | None = None,
    max_sessions: int = MAX_SESSIONS,
) -> dict[str, Any]:
    """Return a bounded per-project timeline from one analytical connection.

    ``con`` must see the control-plane tables (``harness_sessions``,
    ``session_usage``); callers attach them with
    ``attached_control_plane_snapshot``.
    """
    if project_key is not None:
        owner, sep, name = project_key.partition("/")
        if not sep or not owner or not name or "/" in name:
            raise ValueError("project_key must be one <owner>/<name> pair")
    days = max(1, min(int(days), MAX_DAYS))
    max_sessions = max(1, min(int(max_sessions), MAX_SESSIONS))
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = now - timedelta(days=days)
    filters = AnalyticsFilters(days=days, project_key=project_key)

    with materialized_session_facts(con, filters, now) as facts:
        intervals = _rows(
            con,
            f"""SELECT project_key, session_id, started_at, latest_activity_at,
                       total_tokens
                  FROM {facts}
                 WHERE project_key IS NOT NULL
                 ORDER BY latest_activity_at DESC NULLS LAST, session_id
                 LIMIT ?""",
            [_MAX_INTERVALS],
        )
        recent = _rows(
            con,
            f"""SELECT session_id, project_key, harness, host_id, model,
                       started_at, latest_activity_at, total_tokens
                  FROM {facts}
                 WHERE project_key IS NOT NULL
                 ORDER BY latest_activity_at DESC NULLS LAST, session_id
                 LIMIT ?""",
            [max_sessions + 1],
        )

    projects = _project_rollups(intervals, since=since, now=now)
    projects_truncated = len(projects) > MAX_PROJECTS
    projects = projects[:MAX_PROJECTS]
    sessions_truncated = len(recent) > max_sessions
    recent = recent[:max_sessions]
    ids = [row["session_id"] for row in recent]

    harness = _by_session(
        _optional_rows(
            con,
            f"""SELECT * FROM harness_sessions
                 WHERE session_id IN ({_placeholders(ids)})""",
            ids,
        )
    )
    summaries = _by_session(
        _optional_rows(
            con,
            f"""SELECT session_id, ended_at, summary_md, next_steps_md,
                       open_questions
                  FROM session_summaries
                 WHERE session_id IN ({_placeholders(ids)})""",
            ids,
        )
    )
    recaps = _by_session(
        _optional_rows(
            con,
            f"""SELECT session_id, recap_text FROM live_session_recaps
                 WHERE session_id IN ({_placeholders(ids)})""",
            ids,
        )
    )

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    open_items: list[dict[str, Any]] = []
    for row in recent:
        sid = row["session_id"]
        launched = harness.get(sid, {})
        summary = summaries.get(sid, {})
        item = _session_item(
            row, launched=launched, summary=summary, recap=recaps.get(sid), now=now
        )
        day_of = _aware(row.get("started_at")) or _aware(row.get("latest_activity_at"))
        by_day[day_of.date().isoformat() if day_of else "unknown"].append(item)
        open_items.extend(_open_items(item, summary))

    open_items.sort(key=lambda entry: entry.get("at") or "", reverse=True)
    open_truncated = len(open_items) > MAX_OPEN_ITEMS
    return {
        "source": "drover_sessions",
        "project_key": project_key,
        "window": {
            "since": since.isoformat(),
            "until": now.isoformat(),
            "days": days,
        },
        "projects": projects,
        "days": [
            {"date": day, "sessions": by_day[day]}
            for day in sorted(by_day, reverse=True)
        ],
        "open_items": open_items[:MAX_OPEN_ITEMS],
        "truncated": {
            "projects": projects_truncated,
            "sessions": sessions_truncated,
            "open_items": open_truncated,
        },
        "limits": {
            "max_days": MAX_DAYS,
            "max_projects": MAX_PROJECTS,
            "max_sessions": max_sessions,
            "max_open_items": MAX_OPEN_ITEMS,
            "text_chars": TEXT_CHARS,
        },
    }


def _placeholders(ids: list[str]) -> str:
    # `IN ()` is a syntax error; a NULL keeps an empty list a valid no-match.
    return ", ".join("?" for _ in ids) if ids else "NULL"


def _by_session(rows: list[dict]) -> dict[str, dict]:
    return {str(row["session_id"]): row for row in rows if row.get("session_id")}


def _project_rollups(
    rows: list[dict], *, since: datetime, now: datetime
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["project_key"]
        entry = grouped.setdefault(
            key,
            {
                "project_key": key,
                "session_count": 0,
                "total_tokens": 0,
                "token_session_count": 0,
                "last_activity_at": None,
                "_intervals": [],
            },
        )
        entry["session_count"] += 1
        if row.get("total_tokens") is not None:
            entry["total_tokens"] += int(row["total_tokens"])
            entry["token_session_count"] += 1
        start = _aware(row.get("started_at"))
        end = _aware(row.get("latest_activity_at")) or start
        if start is None:
            start = end
        if start is not None and end is not None:
            lo, hi = max(start, since), min(max(end, start), now)
            if hi > lo:
                entry["_intervals"].append((lo, hi))
            if entry["last_activity_at"] is None or end > entry["last_activity_at"]:
                entry["last_activity_at"] = end
    projects = []
    for entry in grouped.values():
        entry["active_hours"] = _active_hours(entry.pop("_intervals"))
        last = entry["last_activity_at"]
        entry["last_activity_at"] = last.isoformat() if last else None
        projects.append(entry)
    projects.sort(
        key=lambda p: (p["last_activity_at"] or "", p["session_count"]), reverse=True
    )
    return projects


def _session_item(
    row: dict[str, Any],
    *,
    launched: dict[str, Any],
    summary: dict[str, Any],
    recap: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    started = _aware(row.get("started_at"))
    last = _aware(row.get("latest_activity_at"))
    if launched:
        state = session_state(
            status=launched.get("status"),
            awaiting=launched.get("awaiting"),
            last_activity=last,
            now=now,
        )
    elif summary:
        # Observed natively (not launched by Drover) and already summarized.
        state = "done"
    else:
        # Observed natively: Drover holds no lifecycle for it, so quiet means
        # ended, never "stuck".
        live = session_state(status=None, awaiting=None, last_activity=last, now=now)
        state = "running" if live == "running" else "ended"
    duration = (
        int(max((last - started).total_seconds(), 0)) if started and last else None
    )
    title = first_line((recap or {}).get("recap_text")) or first_line(
        summary.get("summary_md")
    )
    refs: dict[str, Any] = {}
    if launched.get("branch"):
        refs["branch"] = launched["branch"]
    if launched.get("handoff_mode") == "factory_observer":
        source = str(launched.get("source_session_id") or "")
        if source.startswith("factory/") and "@" in source:
            refs["factory_run_id"] = source.removeprefix("factory/").rpartition("@")[0]
    if launched.get("parent_session_id"):
        refs["parent_session_id"] = launched["parent_session_id"]
    return {
        "session_id": row["session_id"],
        "project_key": row["project_key"],
        "title": title,
        "harness": row.get("harness") or launched.get("harness"),
        "host_id": row.get("host_id") or launched.get("host_id"),
        "launched_by_drover": bool(launched),
        "state": state,
        "started_at": started.isoformat() if started else None,
        "last_activity_at": last.isoformat() if last else None,
        "duration_seconds": duration,
        "total_tokens": row.get("total_tokens"),
        "summary": _clip(summary.get("summary_md")),
        "last_error": _clip(launched.get("last_error")),
        "refs": refs,
    }


def _open_items(item: dict[str, Any], summary: dict[str, Any]) -> list[dict]:
    base = {
        "session_id": item["session_id"],
        "project_key": item["project_key"],
        "at": item["last_activity_at"],
    }
    items: list[dict[str, Any]] = []
    if item["state"] in STUCK_REASONS:
        items.append(
            {
                **base,
                "kind": item["state"],
                "text": item["last_error"] or STUCK_REASONS[item["state"]],
            }
        )
    next_steps = _clip(summary.get("next_steps_md"))
    if next_steps:
        items.append({**base, "kind": "next_step", "text": next_steps})
    for question in (summary.get("open_questions") or [])[:3]:
        text = _clip(question)
        if text:
            items.append({**base, "kind": "open_question", "text": text})
    return items
