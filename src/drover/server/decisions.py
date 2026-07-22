"""Deterministic decision extraction from explicitly marked span attributes.

This is intentionally conservative: it does not infer decisions from arbitrary
prompt/response text. A span becomes a decision source only when its
``attributes_json`` contains an explicit decision statement key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from drover.schema import bootstrap
from drover.server.db import open_duckdb_connection

# "nexus.*" span attribute keys are data schema — do not rename (porting-and-cutover.md §4).
_DECISION_ID_KEYS = (
    "nexus.decision.id",
    "decision.id",
)
_DECISION_STATEMENT_KEYS = (
    "nexus.decision.statement",
    "decision.statement",
    "decision_statement",
)
_DECISION_RATIONALE_KEYS = (
    "nexus.decision.rationale",
    "decision.rationale",
    "decision_rationale",
)
_DECISION_ALTERNATIVES_KEYS = (
    "nexus.decision.alternatives",
    "decision.alternatives",
    "decision_alternatives",
)
_DECISION_SELECTED_ACTION_KEYS = (
    "nexus.decision.selected_action",
    "decision.selected_action",
    "selected_action",
)


@dataclass
class SpanRow:
    trace_id: str | None
    span_id: str | None
    parent_span_id: str | None
    name: str | None
    start_time: Any
    session_id: str | None
    task_id: str | None
    agent_id: str | None
    repo_owner: str | None
    repo_name: str | None
    branch: str | None
    attributes: dict[str, Any]


def _attrs(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_text(attrs: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
        else:
            text = str(value).strip()
        if text:
            return text
    return None


def _alternatives(attrs: dict[str, Any]) -> list[str]:
    for key in _DECISION_ALTERNATIVES_KEYS:
        value = attrs.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return [part.strip() for part in stripped.split("\n") if part.strip()]
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
            return [stripped]
        return [str(value).strip()] if str(value).strip() else []
    return []


def _decision_id(
    attrs: dict[str, Any],
    *,
    trace_id: str | None,
    source_span_id: str | None,
    statement: str,
) -> str:
    explicit = _first_text(attrs, _DECISION_ID_KEYS)
    if explicit:
        return explicit
    digest = hashlib.sha256(
        f"{trace_id or ''}\0{source_span_id or ''}\0{statement}".encode("utf-8")
    ).hexdigest()[:16]
    return f"span-decision-{digest}"


def _is_agent_turn(span: SpanRow) -> bool:
    if (span.name or "").lower() == "agent_turn":
        return True
    activity_type = str(span.attributes.get("prov.activity.type", "")).lower()
    return activity_type == "agent_turn"


def _root_for(
    span: SpanRow, by_key: dict[tuple[str | None, str | None], SpanRow]
) -> SpanRow:
    current = span
    seen: set[tuple[str | None, str | None]] = set()
    while current.parent_span_id:
        key = (current.trace_id, current.parent_span_id)
        if key in seen:
            break
        seen.add(key)
        parent = by_key.get(key)
        if parent is None:
            break
        current = parent
    return current


def _selected_action(
    source: SpanRow,
    spans: list[SpanRow],
    root: SpanRow,
) -> str | None:
    explicit = _first_text(source.attributes, _DECISION_SELECTED_ACTION_KEYS)
    if explicit:
        return explicit

    child_tools = [
        span
        for span in spans
        if span.trace_id == source.trace_id
        and span.parent_span_id == root.span_id
        and (span.name or "").lower() == "tool_call"
        and span.start_time is not None
        and source.start_time is not None
        and span.start_time >= source.start_time
    ]
    child_tools.sort(key=lambda span: (span.start_time, span.span_id or ""))
    if not child_tools:
        return None
    tool = child_tools[0]
    tool_name = (
        _first_text(
            tool.attributes,
            ("tool.name", "gen_ai.tool.name", "nexus.tool.name", "name"),
        )
        or "tool_call"
    )
    action = _first_text(
        tool.attributes,
        ("tool.action", "tool.input", "nexus.tool.action", "arguments"),
    )
    return f"{tool_name}: {action}" if action else tool_name


def derive_decisions(*, duckdb_path: Path, parquet_dir: Path) -> int:
    """Insert deterministic decisions derived from marked spans.

    Returns the number of newly inserted rows. Existing ``decision_id`` rows are
    left unchanged, making the job safe to rerun.
    """

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = open_duckdb_connection(duckdb_path)
    try:
        rows = con.execute("""
            SELECT trace_id, span_id, parent_span_id, name, start_time,
                   session_id, task_id, agent_id, repo_owner, repo_name, branch,
                   attributes_json
            FROM spans_enriched
            WHERE date IS NULL OR date <> '_seed'
            ORDER BY trace_id, start_time NULLS LAST, span_id
            """).fetchall()
        spans = [
            SpanRow(
                trace_id=row[0],
                span_id=row[1],
                parent_span_id=row[2],
                name=row[3],
                start_time=row[4],
                session_id=row[5],
                task_id=row[6],
                agent_id=row[7],
                repo_owner=row[8],
                repo_name=row[9],
                branch=row[10],
                attributes=_attrs(row[11]),
            )
            for row in rows
        ]
        by_key = {(span.trace_id, span.span_id): span for span in spans if span.span_id}

        inserted = 0
        for source in spans:
            statement = _first_text(source.attributes, _DECISION_STATEMENT_KEYS)
            if not statement:
                continue
            root = _root_for(source, by_key)
            if not _is_agent_turn(root):
                continue
            decision_id = _decision_id(
                source.attributes,
                trace_id=source.trace_id,
                source_span_id=source.span_id,
                statement=statement,
            )
            if con.execute(
                "SELECT 1 FROM decisions WHERE decision_id=?", [decision_id]
            ).fetchone():
                continue
            con.execute(
                """
                INSERT INTO decisions (
                  decision_id, trace_id, root_span_id, source_span_id,
                  decision_statement, rationale, alternatives, selected_action,
                  session_id, task_id, agent_id, repo_owner, repo_name, branch,
                  decided_at, extracted_at, extractor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?)
                """,
                [
                    decision_id,
                    source.trace_id,
                    root.span_id,
                    source.span_id,
                    statement,
                    _first_text(source.attributes, _DECISION_RATIONALE_KEYS),
                    _alternatives(source.attributes),
                    _selected_action(source, spans, root),
                    source.session_id,
                    source.task_id,
                    source.agent_id,
                    source.repo_owner,
                    source.repo_name,
                    source.branch,
                    source.start_time,
                    "explicit-span-attributes-v1",
                ],
            )
            inserted += 1
        return inserted
    finally:
        con.close()
