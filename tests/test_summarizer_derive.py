"""Tests for the deterministic summarizer derivations."""

from __future__ import annotations

import json

import duckdb

from drover.server.summarizer.derive import compute_files_touched, compute_tools_used
from drover.server.summarizer.worker import _tool_projection_sql


def _ev(raw_data: dict) -> dict:
    return {"raw_data": json.dumps(raw_data)}


def test_files_touched_from_edit_blocks() -> None:
    events = [
        _ev(
            {
                "tool_use_blocks": [
                    {"name": "Edit", "input": {"file_path": "src/foo.py"}},
                    {"name": "Write", "input": {"file_path": "src/bar.py"}},
                ]
            }
        ),
        _ev(
            {
                "tool_use_blocks": [
                    {"name": "Edit", "input": {"file_path": "src/foo.py"}},  # dup
                ]
            }
        ),
    ]
    files = compute_files_touched(events)
    assert files == ["src/bar.py", "src/foo.py"]


def test_files_touched_path_aliases() -> None:
    events = [
        _ev(
            {
                "tool_use_blocks": [
                    {"name": "Read", "input": {"path": "docs/spec.md"}},
                ]
            }
        ),
    ]
    assert compute_files_touched(events) == ["docs/spec.md"]


def test_files_touched_skips_malformed() -> None:
    events = [
        {"raw_data": "not json"},
        {"raw_data": ""},
        {"raw_data": json.dumps({"tool_use_blocks": "not a list"})},
        {"raw_data": json.dumps({"tool_use_blocks": [{"name": "Edit"}]})},  # no input
    ]
    assert compute_files_touched(events) == []


def test_tools_used_counter() -> None:
    events = [
        _ev(
            {
                "tool_use_blocks": [
                    {"name": "Edit", "input": {}},
                    {"name": "Edit", "input": {}},
                    {"name": "Bash", "input": {}},
                ]
            }
        ),
        _ev(
            {
                "tool_use_blocks": [
                    {"name": "Edit", "input": {}},
                    {"name": "Read", "input": {}},
                ]
            }
        ),
    ]
    assert compute_tools_used(events) == {"Edit": 3, "Bash": 1, "Read": 1}


def test_tools_used_empty() -> None:
    assert compute_tools_used([]) == {}


def test_tools_used_handles_missing_blocks() -> None:
    events = [
        _ev({}),
        _ev({"tool_use_blocks": []}),
        {"raw_data": "garbage"},
    ]
    assert compute_tools_used(events) == {}


def test_tool_projection_excludes_large_unneeded_raw_fields() -> None:
    """S3 pages deterministic tool facts, never an arbitrary tool transcript."""
    huge = "x" * (2 * 1024 * 1024)
    raw = json.dumps(
        {
            "tool_use_blocks": [
                {"name": "Edit", "input": {"path": "src/main.py", "command": huge}}
            ],
            "details": huge,
        }
    )
    with duckdb.connect() as con:
        con.execute("CREATE TABLE events(raw_data VARCHAR)")
        con.execute("INSERT INTO events VALUES (?)", [raw])
        projected = con.execute(
            f"SELECT {_tool_projection_sql()} AS raw_data FROM events"
        ).fetchone()[0]
    assert len(projected.encode()) < 1024
    assert json.loads(projected) == {
        "tool_name": "Edit",
        "tool_use_blocks": [
            {"name": "Edit", "input": {"path": "src/main.py", "file_path": None}}
        ],
        "tool": {"name": None, "input": {"path": None, "file_path": None}},
        "input": {"path": None, "file_path": None},
        "arguments": {"path": None, "file_path": None},
        "path": None,
        "file_path": None,
    }
