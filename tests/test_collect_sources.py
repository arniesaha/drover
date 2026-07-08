"""Tests for collect.sources — file selection + parsing per source."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from drover.collect.sources import (
    ClaudeCodeSource,
    ClaudeMacMiniSource,
    HermesSource,
    OpenClawSource,
    PiMonoSource,
    write_events_jsonl,
)
from drover.models import AgentEvent

FIXTURES = Path(__file__).parent / "fixtures" / "collect"


# --- ClaudeCodeSource ---


def test_claude_code_source_lists_jsonl_files(tmp_path: Path) -> None:
    src = ClaudeCodeSource(root=FIXTURES / "claude_code")
    files = src.list_files_since(watermark=None)
    assert any(f.name == "session-1.jsonl" for f in files)


def test_claude_code_source_excludes_files_older_than_watermark(tmp_path: Path) -> None:
    # Copy fixture to tmp so we can stamp mtimes deterministically
    (tmp_path / "proj").mkdir()
    a = tmp_path / "proj" / "old.jsonl"
    b = tmp_path / "proj" / "new.jsonl"
    a.write_text("{}\n")
    b.write_text("{}\n")
    import os

    os.utime(a, (1700000000, 1700000000))  # 2023
    os.utime(b, (1800000000, 1800000000))  # 2027

    cutoff = datetime.fromtimestamp(1750000000, tz=timezone.utc)  # 2025
    src = ClaudeCodeSource(root=tmp_path)
    files = src.list_files_since(watermark=cutoff)
    names = {f.name for f in files}
    assert "new.jsonl" in names
    assert "old.jsonl" not in names


def test_claude_code_source_parse_yields_agent_events() -> None:
    src = ClaudeCodeSource(root=FIXTURES / "claude_code")
    events = list(src.parse(FIXTURES / "claude_code" / "proj-a" / "session-1.jsonl"))
    assert len(events) == 2
    assert all(isinstance(e, AgentEvent) for e in events)
    assert events[0].session_id == "cc-sess-1"


def test_agent_event_token_usage_tolerates_mixed_shapes() -> None:
    """Newer Claude Code releases write nested dicts ({ephemeral_5m_input_tokens:...}),
    bare strings ('standard'), and arrays into token_usage. The model must
    accept whatever upstream emits — losing one event per shape change is
    not acceptable."""
    cases = [
        {"input_tokens": 100, "output_tokens": 50},  # legacy int-valued
        {"input_tokens": {"ephemeral_5m_input_tokens": 263}},  # nested dict
        {"speed": "standard"},  # str
        {"iterations": []},  # list
        {"inference_geo": ""},  # empty str
    ]
    for tu in cases:
        AgentEvent(
            id="x",
            session_id="s",
            timestamp=datetime.now(tz=timezone.utc),
            agent_id="a",
            event_type="assistant_message",
            token_usage=tu,
        )


def test_claude_code_source_uses_configured_agent_id() -> None:
    """Regression: agent_id used to be hardcoded to "nas-claude" so every
    Claude Code session got the same tag regardless of which machine ran
    the shipper. Now the host_id from collect.toml is threaded through."""
    src = ClaudeCodeSource(
        root=FIXTURES / "claude_code",
        agent_id="my-laptop-claude",
    )
    events = list(src.parse(FIXTURES / "claude_code" / "proj-a" / "session-1.jsonl"))
    assert events
    assert all(e.agent_id == "my-laptop-claude" for e in events)


def test_claude_macmini_source_uses_configured_agent_id() -> None:
    src = ClaudeMacMiniSource(
        root=FIXTURES / "claude_code",
        agent_id="other-host",
    )
    events = list(src.parse(FIXTURES / "claude_code" / "proj-a" / "session-1.jsonl"))
    assert events
    assert all(e.agent_id == "other-host" for e in events)


# --- HermesSource ---


def test_hermes_source_parses_session_json() -> None:
    src = HermesSource(root=FIXTURES / "hermes" / "sessions")
    files = src.list_files_since(watermark=None)
    assert files, "should find at least one hermes session fixture"
    events = list(src.parse(files[0]))
    assert len(events) == 2
    assert events[0].agent_id == "macmini-hermes"


# --- OpenClawSource ---


def test_openclaw_source_parses_jsonl() -> None:
    src = OpenClawSource(root=FIXTURES / "openclaw" / "sessions")
    files = src.list_files_since(watermark=None)
    assert files
    events = list(src.parse(files[0]))
    assert len(events) == 2
    assert events[0].session_id == "oc-sess-1"
    assert events[0].agent_id == "nas-openclaw"


# --- PiMonoSource (sqlite) ---


def test_pi_mono_source_parses_journal_db(tmp_path: Path) -> None:
    db_path = tmp_path / "task-journal.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            type TEXT,
            source TEXT,
            payload TEXT,
            status TEXT,
            result TEXT,
            created_at INTEGER
        )""")
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
        ("t1", "note", "cli", json.dumps({"message": "hi"}), "done", None, 1746792000),
    )
    conn.commit()
    conn.close()

    src = PiMonoSource(db_path=db_path)
    files = src.list_files_since(watermark=None)
    assert files == [db_path]
    events = list(src.parse(db_path))
    assert len(events) == 1
    assert events[0].agent_id == "max-pimono"


def test_pi_mono_source_missing_db_returns_no_files(tmp_path: Path) -> None:
    src = PiMonoSource(db_path=tmp_path / "missing.db")
    assert src.list_files_since(watermark=None) == []


# --- write_events_jsonl ---


def test_write_events_jsonl_atomic(tmp_path: Path) -> None:
    src = ClaudeCodeSource(root=FIXTURES / "claude_code")
    events = list(src.parse(FIXTURES / "claude_code" / "proj-a" / "session-1.jsonl"))
    out = write_events_jsonl(
        events, tmp_path, run_id="20260509T0100", source_id="claude_code"
    )

    assert out.exists()
    assert out.suffix == ".jsonl"
    assert "claude_code" in out.name
    assert "20260509T0100" in out.name
    assert not list(tmp_path.glob("*.tmp")), "no .tmp files should remain"

    # JSONL parses back via TypeAdapter
    adapter = TypeAdapter(AgentEvent)
    rows = [
        adapter.validate_json(line)
        for line in out.read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 2
    assert rows[0].session_id == "cc-sess-1"


def test_write_events_jsonl_empty_returns_none(tmp_path: Path) -> None:
    out = write_events_jsonl([], tmp_path, run_id="r1", source_id="claude_code")
    assert out is None
    assert list(tmp_path.iterdir()) == []


def test_write_events_jsonl_enriches_repo_attribution(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:someowner/myrepo.git"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"], cwd=repo, check=True
    )

    event = AgentEvent(
        id="e1",
        session_id="s1",
        agent_id="test-agent",
        timestamp=datetime(2026, 5, 18, tzinfo=timezone.utc),
        event_type="user_message",
        raw_data={"cwd": str(repo)},
    )

    staging = tmp_path / "staging"
    out = write_events_jsonl([event], staging, run_id="r1", source_id="claude_code")
    assert out is not None

    adapter = TypeAdapter(AgentEvent)
    rows = [
        adapter.validate_json(line)
        for line in out.read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    raw = rows[0].raw_data
    assert raw["_repo_owner"] == "someowner"
    assert raw["_repo_name"] == "myrepo"
    assert raw["gitBranch"] == "main"


def test_write_events_jsonl_no_attribution_for_missing_path(tmp_path: Path) -> None:
    event = AgentEvent(
        id="e1",
        session_id="s1",
        agent_id="test-agent",
        timestamp=datetime(2026, 5, 18, tzinfo=timezone.utc),
        event_type="user_message",
        raw_data={"cwd": "/nonexistent/path/that/does/not/exist"},
    )

    out = write_events_jsonl([event], tmp_path, run_id="r1", source_id="claude_code")
    assert out is not None

    adapter = TypeAdapter(AgentEvent)
    rows = [
        adapter.validate_json(line)
        for line in out.read_text().splitlines()
        if line.strip()
    ]
    raw = rows[0].raw_data
    assert "_repo_owner" not in raw
    assert "_repo_name" not in raw
