"""Deterministic summary derivations from raw_data tool_use_blocks.

These don't need an LLM — we read what the agent actually did from the
tool-call payloads we already have on disk.
"""

from __future__ import annotations

import json
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
        blocks = data.get("tool_use_blocks")
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
