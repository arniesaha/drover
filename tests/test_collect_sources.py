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
    OpenClawTaskFlowSource,
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
    assert events[0].agent_id == "hermes"


# --- OpenClawSource ---


def test_openclaw_source_parses_jsonl() -> None:
    src = OpenClawSource(root=FIXTURES / "openclaw" / "sessions")
    files = src.list_files_since(watermark=None)
    assert files
    events = list(src.parse(files[0]))
    assert len(events) == 2
    assert events[0].session_id == "oc-sess-1"
    assert events[0].agent_id == "openclaw"


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


# --- OpenClaw managed TaskFlow ---


def _factory_state(
    *, run_id: str = "9b9cc2b3", capability: str = "drover-provenance"
) -> str:
    return json.dumps(
        {
            "version": 1,
            "run": {
                "protocol": "capability-factory.run.v1",
                "run": {"id": run_id, "capability": capability},
                # This intentionally represents the only safe part of state
                # the parser uses; evidence and approval content stay unread.
            },
        }
    )


def _create_taskflow_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE flow_runs (
            flow_id TEXT PRIMARY KEY,
            controller_id TEXT,
            revision INTEGER,
            status TEXT,
            current_step TEXT,
            state_json TEXT,
            created_at INTEGER,
            updated_at INTEGER,
            ended_at INTEGER,
            goal TEXT,
            requester_origin_json TEXT,
            wait_json TEXT
        )""")
    conn.execute(
        """INSERT INTO flow_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "1e0eb224-efdf-4d20-addd-2f324fe67930",
            "capability-factory/taskflow-controller",
            0,
            "blocked",
            "intake",
            _factory_state(),
            1790696800000,
            1790696800000,
            None,
            "private goal must not be collected",
            '{"private":"origin"}',
            '{"private":"approval"}',
        ),
    )
    conn.commit()
    conn.close()


def test_openclaw_taskflow_source_collects_initial_and_changed_status(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "openclaw.sqlite"
    _create_taskflow_db(db_path)
    src = OpenClawTaskFlowSource(db_path=db_path)

    initial = list(src.parse(db_path))
    assert len(initial) == 1
    event = initial[0]
    assert (
        event.id == "openclaw-taskflow:1e0eb224-efdf-4d20-addd-2f324fe67930:revision:0"
    )
    assert event.session_id == "openclaw-taskflow:1e0eb224-efdf-4d20-addd-2f324fe67930"
    assert event.event_type == "system_event"
    assert event.raw_data == {
        "source": "openclaw_taskflow",
        "flow_id": "1e0eb224-efdf-4d20-addd-2f324fe67930",
        "run_id": "9b9cc2b3",
        "status": "blocked",
        "revision": 0,
        "controller": "capability-factory/taskflow-controller",
        "capability": "drover-provenance",
        "created_at": "2026-09-29T15:46:40+00:00",
        "updated_at": "2026-09-29T15:46:40+00:00",
        "current_step": "intake",
    }

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE flow_runs SET status = ?, revision = ?, current_step = ?, updated_at = ?",
        ("waiting", 1, "install", 1790696860000),
    )
    conn.commit()
    conn.close()

    changed = list(src.parse(db_path))
    assert len(changed) == 1
    assert changed[0].id.endswith("revision:1")
    assert changed[0].raw_data["status"] == "waiting"
    assert changed[0].raw_data["current_step"] == "install"


def test_openclaw_taskflow_source_detects_newer_wal_file(tmp_path: Path) -> None:
    db_path = tmp_path / "openclaw.sqlite"
    _create_taskflow_db(db_path)
    wal_path = db_path.with_name("openclaw.sqlite-wal")
    wal_path.write_bytes(b"wal")

    import os

    os.utime(db_path, (1790696800, 1790696800))
    os.utime(wal_path, (1790696860, 1790696860))
    watermark = datetime.fromtimestamp(1790696830, tz=timezone.utc)

    assert OpenClawTaskFlowSource(db_path=db_path).list_files_since(watermark) == [
        db_path
    ]


def test_openclaw_taskflow_source_duplicate_collection_is_idempotent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "openclaw.sqlite"
    _create_taskflow_db(db_path)
    src = OpenClawTaskFlowSource(db_path=db_path)

    first = list(src.parse(db_path))
    second = list(src.parse(db_path))
    assert [event.id for event in second] == [event.id for event in first]
    assert [event.model_dump() for event in second] == [
        event.model_dump() for event in first
    ]


def test_openclaw_taskflow_source_skips_absent_or_malformed_schema(
    tmp_path: Path,
) -> None:
    missing = OpenClawTaskFlowSource(db_path=tmp_path / "missing.sqlite")
    assert missing.list_files_since(None) == []

    malformed = tmp_path / "malformed.sqlite"
    conn = sqlite3.connect(malformed)
    conn.execute("CREATE TABLE flow_runs (flow_id TEXT, status TEXT)")
    conn.commit()
    conn.close()
    assert list(OpenClawTaskFlowSource(db_path=malformed).parse(malformed)) == []

    invalid_state = tmp_path / "invalid-state.sqlite"
    _create_taskflow_db(invalid_state)
    conn = sqlite3.connect(invalid_state)
    conn.execute("UPDATE flow_runs SET state_json = ?", ("not-json",))
    conn.commit()
    conn.close()
    assert (
        list(OpenClawTaskFlowSource(db_path=invalid_state).parse(invalid_state)) == []
    )
