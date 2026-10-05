"""Deterministic summary derivations from raw_data tool_use_blocks.

These don't need an LLM — we read what the agent actually did from the
tool-call payloads we already have on disk.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Iterable

_PATH_KEYS = ("file_path", "path")


def _iter_tool_use_blocks(events: Iterable[dict]) -> Iterable[dict]:
    for ev in events:
        raw = ev.get("raw_data")
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        changes = data.get("changes") or (
            data.get("item", {}).get("changes")
            if isinstance(data.get("item"), dict)
            else None
        )
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, dict) and change.get("path"):
                    yield {"name": "file_change", "input": {"path": change["path"]}}
        blocks = data.get("tool_use_blocks")
        if not blocks and ev.get("event_type") in {
            "tool_action",
            "command",
            "file_change",
            "tool_call",
        }:
            nested = data.get("tool") if isinstance(data.get("tool"), dict) else data
            name = (
                nested.get("name")
                or nested.get("tool_name")
                or data.get("tool_name")
                or (data.get("tool") if isinstance(data.get("tool"), str) else None)
            )
            inp = (
                nested.get("input")
                or nested.get("arguments")
                or data.get("input")
                or {}
            )
            if isinstance(inp, str):
                try:
                    inp = json.loads(inp)
                except json.JSONDecodeError:
                    inp = {}
            blocks = [{"name": name or ev.get("event_type"), "input": inp}]
            if ev.get("event_type") == "file_change":
                blocks[0]["input"] = {"path": data.get("path") or data.get("file_path")}

        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict):
                yield block


def compute_files_touched(events: Iterable[dict]) -> list[str]:
    """Return sorted, distinct file paths referenced by any tool_use block."""
    files: set[str] = set()
    for block in _iter_tool_use_blocks(events):
        inp = block.get("input")
        if not isinstance(inp, dict):
            continue
        patch = inp.get("patch") or inp.get("command") or inp.get("text") or ""
        if isinstance(patch, str):
            files.update(
                re.findall(r"\*\*\* (?:Add|Update|Delete) File: ([^\n]+)", patch)
            )
        for k in _PATH_KEYS:
            v = inp.get(k)
            if isinstance(v, str) and v:
                files.add(v)
    return sorted(files)


def compute_tools_used(events: Iterable[dict]) -> dict[str, int]:
    """Counter over tool_use block names."""
    counter: Counter[str] = Counter()
    for block in _iter_tool_use_blocks(events):
        name = block.get("name")
        if isinstance(name, str) and name:
            counter[name] += 1
    return dict(counter)


# Shared predicate: filter before LIMIT and before generation hashing.
SUBSTANTIVE_SQL = """(
    (role IN ('user', 'assistant', 'tool') AND trim(coalesce(content, '')) <> '')
    OR (event_type IN ('tool_call', 'tool_action', 'tool_result', 'file_change', 'command')
        AND raw_data IS NOT NULL AND raw_data <> '{}')
    OR (json_valid(raw_data) AND json_array_length(json_extract(raw_data, '$.tool_use_blocks')) > 0)
) AND event_type NOT IN ('status', 'system_event', 'metadata')"""


def select_substantive_window(
    con, ctes: str, session_id: str, limit: int = 30
) -> list[dict]:
    cur = con.execute(
        f"""WITH {ctes}, substantive AS (
        -- The summary prompt only needs these fields. In particular it must
        -- never marshal an arbitrary raw tool payload through the bounded
        -- DuckLake child merely because that payload made a turn substantive.
        SELECT role, content, timestamp, event_type, agent_id, id,
               raw_data IS NOT NULL AS has_raw_data
        FROM canonical_agent_events WHERE {SUBSTANTIVE_SQL}
    ), selected AS (
        SELECT * FROM substantive ORDER BY timestamp DESC, id DESC LIMIT ?
    ), final_assistant AS (
        SELECT * FROM substantive WHERE role='assistant'
          AND event_type NOT IN ('tool_call', 'tool_action', 'tool_result')
        ORDER BY timestamp DESC, id DESC LIMIT 1
    )
    SELECT * FROM (
        SELECT * FROM selected UNION SELECT * FROM final_assistant
    ) ORDER BY timestamp, id""",
        [session_id, max(1, limit)],
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def final_references(text: str) -> list[str]:
    """Commit hashes and issue/PR references, including GitHub URL forms."""
    refs = re.findall(r"(?<![\w])[0-9a-f]{7,40}(?![\w])|#[0-9]+", text or "")
    refs.extend(
        "#" + number
        for number in re.findall(
            r"github\.com/[^/\s]+/[^/\s]+/(?:issues|pull)/(\d+)", text or ""
        )
    )
    return list(dict.fromkeys(refs))
