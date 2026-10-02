"""Session Graph: what is working on a piece of work, and where it is stuck.

The graph is built only from launch metadata Drover records itself (#473):
an explicit ``parent_session_id`` set by an orchestrator, a handoff's
``source_session_id``, and the Factory run a session was launched for.
Nothing is inferred from traces. See
docs/design/2026-10-session-graph-and-activity.md.

The span tree below the delegation graph is the legacy view, available only
when the optional span integration is enabled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from drover.server.db import open_duckdb_connection

#: Hard caps: the graph is an answer, not an export.
MAX_GRAPH_NODES = 200
MAX_LINEAGE_DEPTH = 20
#: A running session with no activity for this long is reported as idle.
IDLE_AFTER = timedelta(minutes=30)
_TITLE_CHARS = 160

_DONE_STATUSES = frozenset({"completed", "terminated"})
_FAILED_STATUSES = frozenset({"errored", "failed"})
_FACTORY_MODE = "factory_observer"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def session_state(
    *,
    status: str | None,
    awaiting: str | None,
    last_activity: datetime | None,
    now: datetime,
) -> str:
    """One state a person can act on, derived from recorded session fields.

    ``done`` and ``failed`` are terminal; ``awaiting_input`` and
    ``awaiting_approval`` need a person; ``idle`` is a live session that has
    gone quiet for longer than ``IDLE_AFTER``; ``running`` is everything else.
    """
    if status in _DONE_STATUSES:
        return "done"
    if status in _FAILED_STATUSES:
        return "failed"
    if awaiting == "input":
        return "awaiting_input"
    if awaiting == "approval":
        return "awaiting_approval"
    activity = _aware(last_activity)
    if activity is not None and now - activity > IDLE_AFTER:
        return "idle"
    return "running"


#: States worth surfacing as "stuck", with the reason shown beside them.
STUCK_REASONS = {
    "awaiting_input": "waiting for input",
    "awaiting_approval": "waiting for approval",
    "failed": "failed",
    "idle": "no activity for 30 minutes",
}


def first_line(text: Any, limit: int = _TITLE_CHARS) -> str | None:
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return None


def _factory_run_id(session: Any) -> str | None:
    from drover.server.harness.factory_observer import factory_observer_projection

    projection = factory_observer_projection(session)
    return projection["run_id"] if projection else None


def _link_kind(session: Any) -> str:
    if getattr(session, "parent_session_id", None):
        return "delegated"
    mode = getattr(session, "handoff_mode", None)
    if mode == _FACTORY_MODE:
        return "factory_run"
    if getattr(session, "source_session_id", None):
        return "native_resume" if mode == "native_resume" else "handoff"
    return "root"


def _parent_id(session: Any) -> str | None:
    """The recorded session this one was launched from, if any."""
    parent = getattr(session, "parent_session_id", None)
    if parent:
        return parent
    if getattr(session, "handoff_mode", None) == _FACTORY_MODE:
        return None
    return getattr(session, "source_session_id", None) or None


def delegation_graph_payload(
    registry: Any,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
    max_nodes: int = MAX_GRAPH_NODES,
) -> dict[str, Any] | None:
    """Build the work tree containing ``session_id`` (or a Factory ``run_id``).

    Returns ``None`` when neither the session nor the run is known.
    """
    if (session_id is None) == (run_id is None):
        raise ValueError("pass exactly one of session_id or run_id")
    now = now or datetime.now(timezone.utc)
    max_nodes = max(1, min(int(max_nodes), MAX_GRAPH_NODES))

    sessions: dict[str, Any] = {}
    root: dict[str, Any]
    top_ids: list[str]
    if run_id is not None:
        members = registry.factory_run_sessions(run_id, limit=max_nodes)
        if not members:
            return None
        root = {"kind": "factory_run", "run_id": run_id, "started_by": "factory run"}
        for member in members:
            sessions[member.session_id] = member
        top_ids = [member.session_id for member in members]
    else:
        focus = registry.get_session(session_id)
        if focus is None:
            return None
        current = focus
        seen = {focus.session_id}
        missing_parent: str | None = None
        for _ in range(MAX_LINEAGE_DEPTH):
            parent_id = _parent_id(current)
            if not parent_id or parent_id in seen:
                break
            parent = registry.get_session(parent_id)
            if parent is None:
                # A handoff from a session Drover did not launch (or one that
                # predates the registry): named, never invented.
                missing_parent = parent_id
                break
            seen.add(parent.session_id)
            current = parent
        factory_run = _factory_run_id(current)
        if factory_run is not None:
            run_payload = delegation_graph_payload(
                registry, run_id=factory_run, now=now, max_nodes=max_nodes
            )
            if run_payload is not None:
                run_payload["focus_session_id"] = focus.session_id
                return run_payload
        root = {
            "kind": "session",
            "session_id": current.session_id,
            "started_by": (
                f"handoff from {missing_parent}" if missing_parent else "direct launch"
            ),
        }
        if missing_parent:
            root["unknown_parent_session_id"] = missing_parent
        sessions[current.session_id] = current
        top_ids = [current.session_id]

    truncated = False
    frontier = list(top_ids)
    parent_of: dict[str, str] = {}
    while frontier and not truncated:
        budget = max_nodes - len(sessions)
        if budget <= 0:
            truncated = True
            break
        children = registry.sessions_launched_from(frontier, limit=budget + 1)
        next_frontier: list[str] = []
        for child in children:
            if child.session_id in sessions:
                continue
            if len(sessions) >= max_nodes:
                truncated = True
                break
            sessions[child.session_id] = child
            # Attach under whichever recorded link put it in this graph.
            parent_of[child.session_id] = (
                child.parent_session_id
                if child.parent_session_id in sessions
                else child.source_session_id or ""
            )
            next_frontier.append(child.session_id)
        frontier = next_frontier

    ids = list(sessions)
    recaps = _best_effort(lambda: registry.latest_live_recaps(ids), {})
    missing_titles = [sid for sid in ids if sid not in recaps]
    previews = (
        _best_effort(lambda: registry.latest_session_previews(missing_titles), {})
        if missing_titles
        else {}
    )
    tokens = _best_effort(lambda: registry.session_token_totals(ids), {})

    nodes: dict[str, dict[str, Any]] = {}
    for sid, session in sessions.items():
        recap = recaps.get(sid)
        nodes[sid] = _node(
            session,
            title=first_line(getattr(recap, "text", None))
            or first_line(previews.get(sid)),
            tokens=tokens.get(sid),
            now=now,
        )
    for sid, parent in parent_of.items():
        if parent in nodes:
            nodes[parent]["children"].append(nodes[sid])
    root["children"] = [nodes[sid] for sid in top_ids]
    stuck = [
        {
            "session_id": sid,
            "state": node["state"],
            "reason": STUCK_REASONS[node["state"]],
            "host_id": node["host_id"],
            "last_activity_at": node["last_activity_at"],
        }
        for sid, node in nodes.items()
        if node["state"] in STUCK_REASONS
    ]
    payload: dict[str, Any] = {
        "source": "drover_launch_metadata",
        "generated_at": now.isoformat(),
        "root": root,
        "node_count": len(nodes),
        "truncated": truncated,
        "max_nodes": max_nodes,
        "stuck": stuck,
    }
    if session_id is not None:
        payload["focus_session_id"] = session_id
    return payload


def _best_effort(read, default):
    try:
        return read()
    except Exception:  # noqa: BLE001 - titles and tokens decorate, never block
        return default


def _node(session: Any, *, title: str | None, tokens: int | None, now: datetime):
    last_activity = (
        getattr(session, "last_activity", None)
        or getattr(session, "updated_at", None)
        or getattr(session, "started_at", None)
    )
    repo = (
        f"{session.repo_owner}/{session.repo_name}"
        if getattr(session, "repo_owner", None) and getattr(session, "repo_name", None)
        else None
    )
    return {
        "session_id": session.session_id,
        "link": _link_kind(session),
        "title": title,
        "harness": session.harness,
        "host_id": session.host_id,
        "state": session_state(
            status=session.status,
            awaiting=getattr(session, "awaiting", None),
            last_activity=last_activity,
            now=now,
        ),
        "status": session.status,
        "started_at": _iso(getattr(session, "started_at", None)),
        "last_activity_at": _iso(last_activity),
        "ended_at": _iso(getattr(session, "ended_at", None)),
        "repo": repo,
        "branch": getattr(session, "branch", None),
        "last_error": getattr(session, "last_error", None),
        "total_tokens": tokens,
        "children": [],
    }


def _graph_label(node: dict[str, Any]) -> str:
    parts = [
        node["session_id"],
        f"[{node['state']}]",
        f"{node.get('harness') or '?'}@{node.get('host_id') or '?'}",
    ]
    if node.get("link") not in (None, "root"):
        parts.append(f"({node['link']})")
    if node.get("title"):
        parts.append(f"— {node['title']}")
    return " ".join(parts)


def format_graph_ascii(payload: dict[str, Any]) -> str:
    root = payload["root"]
    if root["kind"] == "factory_run":
        header = f"factory run {root['run_id']}"
        tops = root["children"]
        lines = [header]
        for idx, node in enumerate(tops):
            _append_graph_ascii(lines, node, prefix="", is_last=idx == len(tops) - 1)
    else:
        top = root["children"][0]
        lines = [f"{_graph_label(top)}  (started by: {root['started_by']})"]
        children = top["children"]
        for idx, node in enumerate(children):
            _append_graph_ascii(
                lines, node, prefix="", is_last=idx == len(children) - 1
            )
    if payload.get("truncated"):
        lines.append(f"… truncated at {payload['max_nodes']} sessions")
    if payload.get("stuck"):
        lines.append("")
        lines.append("stuck:")
        for item in payload["stuck"]:
            lines.append(f"  {item['session_id']}: {item['reason']}")
    return "\n".join(lines) + "\n"


def _append_graph_ascii(
    lines: list[str], node: dict[str, Any], *, prefix: str, is_last: bool
) -> None:
    lines.append(f"{prefix}{'└─ ' if is_last else '├─ '}{_graph_label(node)}")
    child_prefix = prefix + ("   " if is_last else "│  ")
    children = node.get("children", [])
    for idx, child in enumerate(children):
        _append_graph_ascii(
            lines, child, prefix=child_prefix, is_last=idx == len(children) - 1
        )


def format_graph_dot(payload: dict[str, Any]) -> str:
    lines = ["digraph session_graph {", "  rankdir=LR;"]
    root = payload["root"]

    def visit(node: dict[str, Any]) -> None:
        sid = _dot_escape(node["session_id"])
        lines.append(f'  "{sid}" [label="{_dot_escape(_graph_label(node))}"];')
        for child in node.get("children", []):
            lines.append(f'  "{sid}" -> "{_dot_escape(child["session_id"])}";')
            visit(child)

    if root["kind"] == "factory_run":
        run = _dot_escape(f"factory/{root['run_id']}")
        lines.append(f'  "{run}" [shape=box];')
        for node in root["children"]:
            lines.append(f'  "{run}" -> "{_dot_escape(node["session_id"])}";')
    for node in root["children"]:
        visit(node)
    lines.append("}")
    return "\n".join(lines) + "\n"


# --- Legacy span tree (optional span integration only) ------------------------


def load_session_spans(
    duckdb_path: Path,
    parquet_dir: Path,
    session_id: str,
    *,
    max_spans: int = 5000,
) -> list[dict[str, Any]]:
    """Load spans for one session from bounded span date partitions.

    The query is intentionally partition-by-partition and avoids ``spans_enriched``
    so graphing does not trigger attribution joins or broad scans over unrelated
    event partitions.
    """

    partitions = sorted(
        path.name.removeprefix("date=")
        for path in (parquet_dir / "spans").glob("date=*")
        if path.name != "date=_seed" and any(path.glob("*.parquet"))
    )
    if not partitions:
        return [], False

    con = open_duckdb_connection(duckdb_path, role="diagnostic")
    try:
        rows = []
        for partition_date in partitions:
            remaining = max_spans + 1 - len(rows)
            if remaining <= 0:
                break
            rows.extend(
                con.execute(
                    """
            SELECT
              trace_id,
              span_id,
              parent_span_id,
              name,
              service_name,
              start_time,
              end_time,
              duration_ms,
              task_id,
              agent_id,
              cost_usd
            FROM spans_for_date(?)
            WHERE session_id = ?
            ORDER BY start_time NULLS LAST, span_id
            LIMIT ?
            """,
                    [partition_date, session_id, remaining],
                ).fetchall()
            )
        rows.sort(key=lambda row: (row[5] is None, row[5], row[1] or ""))
        truncated = len(rows) > max_spans
        rows = rows[:max_spans]
    finally:
        con.close()

    spans: list[dict[str, Any]] = []
    for row in rows:
        (
            trace_id,
            span_id,
            parent_span_id,
            name,
            service_name,
            start_time,
            end_time,
            duration_ms,
            task_id,
            agent_id,
            cost_usd,
        ) = row
        spans.append(
            {
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_span_id": parent_span_id,
                "name": name,
                "service_name": service_name,
                "start_time": _iso(start_time),
                "end_time": _iso(end_time),
                "duration_ms": duration_ms,
                "task_id": task_id,
                "agent_id": agent_id,
                "cost_usd": cost_usd,
                "children": [],
            }
        )
    return spans, truncated


def build_span_forest(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach spans to their parent_span_id and return root nodes."""

    by_id = {
        (span.get("trace_id"), span["span_id"]): span
        for span in spans
        if span.get("span_id")
    }
    roots: list[dict[str, Any]] = []
    for span in spans:
        parent_id = span.get("parent_span_id")
        parent = by_id.get((span.get("trace_id"), parent_id)) if parent_id else None
        if parent is None or parent is span:
            roots.append(span)
        else:
            parent["children"].append(span)
    return roots


def session_graph_payload(
    duckdb_path: Path,
    parquet_dir: Path,
    session_id: str,
    *,
    max_spans: int = 5000,
) -> dict[str, Any]:
    spans, truncated = load_session_spans(
        duckdb_path, parquet_dir, session_id, max_spans=max_spans
    )
    return {
        "session_id": session_id,
        "span_count": len(spans),
        "truncated": truncated,
        "roots": build_span_forest(spans),
    }


def _label(span: dict[str, Any]) -> str:
    name = span.get("name") or "(unnamed)"
    span_id = span.get("span_id") or "?"
    duration_ms = span.get("duration_ms")
    suffix = f" {duration_ms:g}ms" if isinstance(duration_ms, (int, float)) else ""
    return f"{name} [{span_id}]{suffix}"


def format_ascii(payload: dict[str, Any]) -> str:
    lines = [f"session {payload['session_id']} ({payload['span_count']} spans)"]
    roots = payload.get("roots", [])
    for idx, root in enumerate(roots):
        _append_ascii(lines, root, prefix="", is_last=idx == len(roots) - 1)
    if payload.get("truncated"):
        lines.append("… truncated at max spans")
    return "\n".join(lines) + "\n"


def _append_ascii(
    lines: list[str], span: dict[str, Any], *, prefix: str, is_last: bool
) -> None:
    connector = "└─ " if is_last else "├─ "
    lines.append(f"{prefix}{connector}{_label(span)}")
    children = span.get("children", [])
    child_prefix = prefix + ("   " if is_last else "│  ")
    for idx, child in enumerate(children):
        _append_ascii(
            lines, child, prefix=child_prefix, is_last=idx == len(children) - 1
        )


def _dot_escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def format_dot(payload: dict[str, Any]) -> str:
    lines = ["digraph session_spans {", "  rankdir=LR;"]

    def visit(span: dict[str, Any]) -> None:
        span_id = span.get("span_id") or "?"
        lines.append(
            f'  "{_dot_escape(span_id)}" [label="{_dot_escape(_label(span))}"];'
        )
        for child in span.get("children", []):
            child_id = child.get("span_id") or "?"
            lines.append(f'  "{_dot_escape(span_id)}" -> "{_dot_escape(child_id)}";')
            visit(child)

    for root in payload.get("roots", []):
        visit(root)
    lines.append("}")
    return "\n".join(lines) + "\n"
