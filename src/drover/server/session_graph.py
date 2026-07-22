"""Session span-tree reconstruction helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from drover.server.db import open_duckdb_connection


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


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
