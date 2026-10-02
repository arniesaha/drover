"""Session history: keyset-paged, filtered, searchable, from PostgreSQL only.

``GET /sessions/history`` pages every session the control plane knows, across
hosts (retired ones included), newest activity first. See
docs/design/session-history.md for the contract and the limits table.

Bounded by construction: at most ``MAX_PAGE_SIZE + 1`` rows are read, each row
is a fixed shape with capped text, and the serialized page is held under
``RESPONSE_BYTES``. Pages never use OFFSET: the cursor is the last row's
``(activity_at, session_id)`` and the next page starts strictly below it, so
rows arriving above the cursor can never shift a later page.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from drover.server.harness.registry import (
    ARCHIVED_SESSION_STATUSES,
    HarnessRegistry,
)
from drover.server.response_caps import ResponseCaps, bound_text, fit_page

DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 50
RESPONSE_BYTES = 64 * 1024
TITLE_CHARS = 120
SUMMARY_CHARS = 300
MAX_FILTER_VALUES = 20
MAX_QUERY_CHARS = 200
MAX_QUERY_TERMS = 8
MAX_FACET_REPOS = 50
MAX_FACET_HOSTS = 100
MAX_FACET_HARNESSES = 20
#: Transcript pages a history client may request from the existing
#: ``/harness/sessions/<id>/messages`` endpoint.
TRANSCRIPT_PAGE_SIZE = 200

# One byte is the trailing newline the HTTP layer appends.
CAPS = ResponseCaps(rows=MAX_PAGE_SIZE, response_bytes=RESPONSE_BYTES - 1)

STATES = ("running", "awaiting", "finished", "failed")
_FINISHED = ("completed", "terminated")
_FAILED = ("errored", "failed")
_CURSOR_VERSION = 1
# The one sort key. It must match the index expression in migration 9.
_ACTIVITY = "COALESCE(s.last_activity, s.updated_at)"
_FILTER_KEYS = frozenset(
    {"host", "harness", "repo", "state", "since", "until", "q", "limit", "cursor"}
)


class HistoryUnavailable(RuntimeError):
    """History needs the PostgreSQL control store; this hub runs DuckDB."""


@dataclass(frozen=True)
class HistoryQuery:
    hosts: tuple[str, ...] = ()
    harnesses: tuple[str, ...] = ()
    repos: tuple[tuple[str, str], ...] = ()
    states: tuple[str, ...] = ()
    since: datetime | None = None
    until: datetime | None = None
    q: str | None = None
    limit: int = DEFAULT_PAGE_SIZE
    cursor: tuple[datetime, str] | None = field(default=None, compare=False)

    def fingerprint(self) -> str:
        """Identify the filter set, so a cursor cannot be replayed under another."""
        filters = asdict(self)
        filters.pop("limit")
        filters.pop("cursor")
        canonical = json.dumps(filters, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Parsing                                                                     #
# --------------------------------------------------------------------------- #


def _values(params: dict[str, list[str]], name: str) -> tuple[str, ...]:
    """Repeated and comma-separated values, deduplicated, order kept."""
    seen: dict[str, None] = {}
    for raw in params.get(name) or ():
        for part in raw.split(","):
            part = part.strip()
            if part:
                seen.setdefault(part, None)
    if len(seen) > MAX_FILTER_VALUES:
        raise ValueError(f"at most {MAX_FILTER_VALUES} {name} values")
    for value in seen:
        if len(value) > 200:
            raise ValueError(f"{name} value is too long")
    return tuple(seen)


def _single(params: dict[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    if not values:
        return None
    if len(values) != 1:
        raise ValueError(f"{name} must appear once")
    return values[0].strip() or None


def _parse_instant(name: str, raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        if len(raw) == 10:
            value = datetime.combine(date.fromisoformat(raw), time.min)
        else:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 date or timestamp") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def parse_history_query(params: dict[str, list[str]]) -> HistoryQuery:
    unknown = set(params) - _FILTER_KEYS
    if unknown:
        raise ValueError(f"unknown parameter: {sorted(unknown)[0]}")
    states = _values(params, "state")
    for state in states:
        if state not in STATES:
            raise ValueError(f"state must be one of {', '.join(STATES)}")
    repos: list[tuple[str, str]] = []
    for repo in _values(params, "repo"):
        owner, sep, name = repo.partition("/")
        if not sep or not owner or not name or "/" in name:
            raise ValueError("repo must be owner/name")
        repos.append((owner, name))
    raw_limit = _single(params, "limit")
    limit = DEFAULT_PAGE_SIZE
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise ValueError("limit must be an integer") from exc
        if limit < 1:
            raise ValueError("limit must be positive")
        # Clamped, not rejected: the cap is a server property a client may
        # not know, and the response states the size it actually used.
        limit = min(limit, MAX_PAGE_SIZE)
    q = _single(params, "q")
    if q is not None and len(q) > MAX_QUERY_CHARS:
        raise ValueError(f"q must be at most {MAX_QUERY_CHARS} characters")
    since = _parse_instant("since", _single(params, "since"))
    until = _parse_instant("until", _single(params, "until"))
    if since is not None and until is not None and until <= since:
        raise ValueError("until must be after since")
    query = HistoryQuery(
        hosts=_values(params, "host"),
        harnesses=_values(params, "harness"),
        repos=tuple(repos),
        states=states,
        since=since,
        until=until,
        q=q,
        limit=limit,
    )
    raw_cursor = _single(params, "cursor")
    if raw_cursor is None:
        return query
    return replace(query, cursor=decode_cursor(raw_cursor, query.fingerprint()))


# --------------------------------------------------------------------------- #
# Cursor                                                                      #
# --------------------------------------------------------------------------- #


def encode_cursor(activity_at: datetime, session_id: str, fingerprint: str) -> str:
    raw = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "t": activity_at.astimezone(timezone.utc).isoformat(),
            "id": session_id,
            "f": fingerprint,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(token: str, fingerprint: str) -> tuple[datetime, str]:
    if len(token) > 512:
        raise ValueError("cursor is invalid")
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if payload.get("v") != _CURSOR_VERSION:
            raise ValueError
        activity_at = datetime.fromisoformat(payload["t"])
        session_id = payload["id"]
        if not isinstance(session_id, str) or activity_at.tzinfo is None:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
        raise ValueError("cursor is invalid") from exc
    if payload.get("f") != fingerprint:
        raise ValueError("cursor belongs to different filters; start again without it")
    return activity_at, session_id


# --------------------------------------------------------------------------- #
# Query                                                                       #
# --------------------------------------------------------------------------- #


def text_search_query(q: str | None) -> str | None:
    """A prefix-matching ``to_tsquery`` string built only from word characters.

    Every term must match (AND) and the last can be a prefix, so the search box
    narrows as the user types. Built from ``[^\\W_]+`` runs, which the
    ``simple`` parser keeps as single lexemes, so no operator can be injected.
    """
    if not q:
        return None
    terms = re.findall(r"[^\W_]+", q.lower())[:MAX_QUERY_TERMS]
    if not terms:
        return None
    return " & ".join(f"{term}:*" for term in terms)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join('?' for _ in values)})"


def _state_predicate(state: str) -> tuple[str, list[str]]:
    archived = list(ARCHIVED_SESSION_STATUSES)
    if state == "finished":
        return _in("s.status", _FINISHED), list(_FINISHED)
    if state == "failed":
        return _in("s.status", _FAILED), list(_FAILED)
    live = f"NOT {_in('s.status', archived)}"
    if state == "awaiting":
        return (
            f"({live} AND (s.status = 'awaiting' OR s.awaiting IS NOT NULL))",
            archived,
        )
    # Anything unrecognised counts as live, matching ARCHIVED_SESSION_STATUSES.
    return (
        f"({live} AND s.status <> 'awaiting' AND s.awaiting IS NULL)",
        archived,
    )


def history_where(query: HistoryQuery) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if query.cursor is not None:
        clauses.append(f"({_ACTIVITY}, s.session_id) < (?, ?)")
        params.extend(query.cursor)
    if query.hosts:
        clauses.append(_in("s.host_id", query.hosts))
        params.extend(query.hosts)
    if query.harnesses:
        clauses.append(_in("s.harness", query.harnesses))
        params.extend(query.harnesses)
    if query.repos:
        clauses.append(
            "("
            + " OR ".join("(s.repo_owner = ? AND s.repo_name = ?)" for _ in query.repos)
            + ")"
        )
        for owner, name in query.repos:
            params.extend((owner, name))
    if query.states:
        parts = []
        for state in query.states:
            clause, state_params = _state_predicate(state)
            parts.append(clause)
            params.extend(state_params)
        clauses.append("(" + " OR ".join(parts) + ")")
    if query.since is not None:
        clauses.append(f"{_ACTIVITY} >= ?")
        params.append(query.since)
    if query.until is not None:
        clauses.append(f"{_ACTIVITY} < ?")
        params.append(query.until)
    tsquery = text_search_query(query.q)
    if tsquery is not None:
        # Hook: a semantic mode would add a second candidate set here from
        # session_embeddings (pgvector), keeping the same keyset order.
        clauses.append(
            "s.session_id IN (SELECT ss.session_id FROM session_search ss "
            "WHERE ss.document @@ to_tsquery('simple', ?))"
        )
        params.append(tsquery)
    return (" AND ".join(clauses) or "TRUE"), params


_PAGE_SQL = """
WITH page AS (
  SELECT s.session_id, s.host_id, s.harness, s.model, s.repo_owner, s.repo_name,
         s.branch, s.cwd, s.status, s.awaiting, s.started_at, s.ended_at,
         s.native_session_id, {activity} AS activity_at
    FROM harness_sessions s
   WHERE {where}
   ORDER BY {activity} DESC, s.session_id DESC
   LIMIT ?
)
SELECT page.session_id, page.host_id, page.harness, page.model, page.repo_owner,
       page.repo_name, page.branch, page.cwd, page.status, page.awaiting,
       page.started_at, page.ended_at, page.activity_at,
       h.display_name AS host_name, h.retired_at AS host_retired_at,
       (h.host_id IS NULL) AS host_missing,
       left(p.content_preview, 400) AS preview,
       mem.summary,
       EXISTS (SELECT 1 FROM harness_events e WHERE e.session_id = page.session_id)
         AS has_transcript,
       u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens
  FROM page
  LEFT JOIN harness_hosts h ON h.host_id = page.host_id
  LEFT JOIN harness_session_previews p ON p.session_id = page.session_id
  LEFT JOIN session_usage u ON u.session_id = page.session_id
  LEFT JOIN LATERAL (
    SELECT left(COALESCE(m.summary_md, m.recap_text), 1200) AS summary
      FROM session_memory m
     WHERE m.session_id IN (page.session_id, page.native_session_id)
     ORDER BY (m.summary_md IS NOT NULL) DESC, (m.session_id = page.session_id) DESC
     LIMIT 1
  ) mem ON TRUE
 ORDER BY page.activity_at DESC, page.session_id DESC
"""


def page_sql(query: HistoryQuery) -> tuple[str, list[Any]]:
    where, params = history_where(query)
    sql = _PAGE_SQL.format(activity=_ACTIVITY, where=where)
    return sql, [*params, query.limit + 1]


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #


def session_state(status: str | None, awaiting: str | None) -> str:
    status = status or ""
    if status in _FINISHED:
        return "finished"
    if status in _FAILED:
        return "failed"
    if status == "awaiting" or awaiting:
        return "awaiting"
    return "running"


def _wire(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


_MARKDOWN_NOISE = re.compile(r"[#*_`>|]+|\[(.*?)\]\([^)]*\)")


def summary_snippet(text: str | None) -> str | None:
    """Plain, single-paragraph text of at most ``SUMMARY_CHARS`` characters."""
    if not text:
        return None
    plain = _MARKDOWN_NOISE.sub(lambda m: m.group(1) or " ", text)
    plain = re.sub(r"^\s*[-+]\s+", "", plain, flags=re.MULTILINE)
    plain = " ".join(plain.split())
    return bound_text(plain, SUMMARY_CHARS) or None


def session_title(row: dict[str, Any]) -> str:
    preview = HarnessRegistry._safe_session_preview(str(row.get("preview") or ""))
    first_line = next(
        (line.strip() for line in preview.splitlines() if line.strip()), ""
    )
    if first_line:
        return bound_text(" ".join(first_line.split()), TITLE_CHARS) or first_line
    if row.get("repo_name"):
        repo = (
            f"{row['repo_owner']}/{row['repo_name']}"
            if row.get("repo_owner")
            else row["repo_name"]
        )
        return f"{repo} · {row['branch']}" if row.get("branch") else repo
    cwd = str(row.get("cwd") or "").rstrip("/")
    if cwd:
        return bound_text(Path(cwd).name or cwd, TITLE_CHARS) or cwd
    return f"{row.get('harness') or 'Agent'} session"


def history_item(row: dict[str, Any]) -> dict[str, Any]:
    tokens = None
    if any(
        row.get(key) is not None
        for key in ("input_tokens", "output_tokens", "cache_read_tokens")
    ):
        tokens = {
            "input": row.get("input_tokens"),
            "output": row.get("output_tokens"),
            "cache_read": row.get("cache_read_tokens"),
            "cache_write": row.get("cache_write_tokens"),
        }
    repo = None
    if row.get("repo_name"):
        repo = (
            f"{row['repo_owner']}/{row['repo_name']}"
            if row.get("repo_owner")
            else row["repo_name"]
        )
    retired = bool(row.get("host_missing")) or row.get("host_retired_at") is not None
    return {
        "id": row["session_id"],
        "title": session_title(row),
        "harness": row.get("harness"),
        "model": bound_text(row.get("model"), 80),
        "host": {
            "id": row.get("host_id"),
            "name": bound_text(row.get("host_name") or row.get("host_id"), 80),
            "retired": retired,
        },
        "repo": bound_text(repo, 160),
        "branch": bound_text(row.get("branch"), 120),
        "state": session_state(row.get("status"), row.get("awaiting")),
        "status": row.get("status"),
        "started_at": _wire(row.get("started_at")),
        "ended_at": _wire(row.get("ended_at")),
        "last_activity": _wire(row.get("activity_at")),
        "summary": summary_snippet(row.get("summary")),
        "has_transcript": bool(row.get("has_transcript")),
        "tokens": tokens,
    }


def _rows(cursor: Any) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _connection(control_path: str | Path):
    from drover.server.control_store import is_postgres_control_store
    from drover.server.db import control_plane_connection

    if not is_postgres_control_store(control_path):
        raise HistoryUnavailable("session history needs the PostgreSQL control store")
    return control_plane_connection(control_path, timeout=2.0)


def fetch_history_page(
    control_path: str | Path, query: HistoryQuery, *, now: datetime | None = None
) -> dict[str, Any]:
    sql, params = page_sql(query)
    with _connection(control_path) as con:
        rows = _rows(con.execute(sql, params))
    more = len(rows) > query.limit
    rows = rows[: query.limit]
    items = [history_item(row) for row in rows]
    fingerprint = query.fingerprint()
    as_of = _wire(now or datetime.now(timezone.utc))

    def envelope(prefix: list[dict[str, Any]], truncated: bool) -> dict[str, Any]:
        has_more = more or len(prefix) < len(items)
        last = rows[len(prefix) - 1] if prefix else None
        return {
            "items": prefix,
            "next_cursor": (
                encode_cursor(last["activity_at"], last["session_id"], fingerprint)
                if has_more and last is not None
                else None
            ),
            "has_more": has_more,
            "page_size": query.limit,
            "truncated": truncated,
            "as_of": as_of,
        }

    body, count = fit_page(envelope, items, CAPS)
    if items and count == 0:  # pragma: no cover - rows are fixed-size, far below
        raise ValueError("a single history row exceeds the response budget")
    return body


def fetch_history_facets(control_path: str | Path) -> dict[str, Any]:
    """Bounded filter-chip values: hosts (retired labelled), harnesses, recent repos."""
    with _connection(control_path) as con:
        hosts = _rows(
            con.execute(
                "SELECT host_id, display_name, retired_at FROM harness_hosts "
                "ORDER BY (retired_at IS NOT NULL), display_name, host_id LIMIT ?",
                [MAX_FACET_HOSTS],
            )
        )
        harnesses = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT harness FROM harness_sessions ORDER BY harness LIMIT ?",
                [MAX_FACET_HARNESSES],
            ).fetchall()
        ]
        repos = [
            f"{row[0]}/{row[1]}"
            for row in con.execute(
                f"""
                SELECT s.repo_owner, s.repo_name, MAX({_ACTIVITY}) AS latest
                  FROM harness_sessions s
                 WHERE s.repo_owner IS NOT NULL AND s.repo_name IS NOT NULL
                 GROUP BY s.repo_owner, s.repo_name
                 ORDER BY latest DESC LIMIT ?
                """,
                [MAX_FACET_REPOS],
            ).fetchall()
        ]
    return {
        "hosts": [
            {
                "id": row["host_id"],
                "name": bound_text(row["display_name"] or row["host_id"], 80),
                "retired": row["retired_at"] is not None,
            }
            for row in hosts
        ],
        "harnesses": harnesses,
        "repos": repos,
        "states": list(STATES),
    }
