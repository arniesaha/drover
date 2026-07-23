"""Tests for the Claude Code structured driver."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from drover.server.harness.structured.claude import (
    ClaudeDriver,
    child_env,
    default_command,
)

FIXTURES_DIR = Path("tests/fixtures/structured")
CLAUDE_FIXTURES = [
    FIXTURES_DIR / "claude_basic.ndjson",
    FIXTURES_DIR / "claude_bidi_capture.ndjson",
    FIXTURES_DIR / "claude_approval.ndjson",
]


def _driver(sink: list) -> ClaudeDriver:
    return ClaudeDriver(["true"], None, sink.append)


def test_default_command_shape():
    command = default_command("/opt/bin/claude")
    assert command[0] == "/opt/bin/claude"
    assert "--output-format" in command and "stream-json" in command


def test_default_command_falls_back_to_which(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    command = default_command(None)
    assert command[0] == "claude"


def test_default_command_uses_versioned_claude_when_path_lookup_fails(
    monkeypatch, tmp_path
):
    versions_dir = tmp_path / ".local/share/claude/versions"
    versions_dir.mkdir(parents=True)
    older = versions_dir / "2.1.196"
    newer = versions_dir / "2.1.201"
    older.write_text("#!/bin/sh\n")
    newer.write_text("#!/bin/sh\n")
    older.chmod(0o755)
    newer.chmod(0o755)

    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    command = default_command(None)
    assert command[0] == str(newer)
    assert "--output-format" in command


def test_versioned_claude_lookup_survives_non_numeric_entries(monkeypatch, tmp_path):
    versions_dir = tmp_path / ".local/share/claude/versions"
    versions_dir.mkdir(parents=True)
    for name in ("latest", "2.1.196", "2.1.201"):
        path = versions_dir / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)

    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    command = default_command(None)
    assert command[0] == str(versions_dir / "2.1.201")


def test_parse_assistant_text_block():
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "hello nexus"},
                ],
            },
        }
    )
    messages = _driver([]).parse_line(line)
    assert [m.type for m in messages] == ["assistant_output"]
    assert messages[0].text == "hello nexus"


def test_parse_thinking_block_maps_to_assistant_output():
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "pondering", "signature": "sig"},
                ],
            },
        }
    )
    messages = _driver([]).parse_line(line)
    assert [m.type for m in messages] == ["assistant_output"]
    assert messages[0].payload["thinking"] is True


def test_parse_tool_use_and_result():
    use = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
        }
    )
    result = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu1", "content": "ok"},
                ],
            },
        }
    )
    driver = _driver([])
    action = driver.parse_line(use)[0]
    assert action.type == "tool_action" and action.payload["tool"] == "Bash"
    outcome = driver.parse_line(result)[0]
    assert outcome.type == "tool_result"


def test_parse_result_marks_turn_complete():
    line = json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.01})
    message = _driver([]).parse_line(line)[0]
    assert message.type == "status"
    assert message.payload["turn_complete"] is True
    assert message.payload["awaiting"] == "input"


def test_parse_system_event_becomes_status():
    line = json.dumps(
        {"type": "system", "subtype": "init", "session_id": "abc", "cwd": "/tmp"}
    )
    message = _driver([]).parse_line(line)[0]
    assert message.type == "status"
    assert message.role == "system"
    assert message.payload["native_session_id"] == "abc"


def test_parse_control_request_becomes_approval_prompt():
    line = json.dumps(
        {
            "type": "control_request",
            "request_id": "req-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "rm -rf /tmp/x"},
            },
        }
    )
    message = _driver([]).parse_line(line)[0]
    assert message.type == "approval_prompt"
    assert message.payload["request_id"] == "req-1"
    assert message.payload["tool"] == "Bash"


def test_unknown_but_valid_json_top_level_kind_maps_to_status_not_raw():
    line = json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed", "isUsingOverage": False},
        }
    )
    message = _driver([]).parse_line(line)[0]
    assert message.type == "status"
    assert message.role == "system"
    assert message.text == "rate_limit_event"
    assert message.payload["type"] == "rate_limit_event"


def test_empty_content_array_falls_back_to_status_not_raw():
    line = json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "content": []}}
    )
    message = _driver([]).parse_line(line)[0]
    assert message.type == "status"
    assert "raw" != message.type


def test_send_turn_and_answer_permission_wire_shapes(monkeypatch):
    sent: list[dict] = []
    driver = _driver([])
    monkeypatch.setattr(driver, "send_line", sent.append)
    driver.send_turn("do the thing", turn_id="t1")
    assert sent[0]["type"] == "user"
    assert sent[0]["message"]["content"][0]["text"] == "do the thing"
    driver.answer_permission("req-1", "allow")
    assert sent[1]["type"] == "control_response"
    assert sent[1]["response"]["request_id"] == "req-1"


def test_answer_permission_deny_behavior(monkeypatch):
    sent: list[dict] = []
    driver = _driver([])
    monkeypatch.setattr(driver, "send_line", sent.append)
    driver.answer_permission("req-2", "deny", note="not allowed")
    assert sent[0]["response"]["response"]["behavior"] == "deny"
    assert sent[0]["response"]["response"]["message"] == "not allowed"


def test_child_env_strips_claude_prefixed_vars(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
    env = child_env()
    assert not any(key.startswith("CLAUDE") for key in env)
    assert "PATH" in env and env["PATH"]
    assert env["SOME_OTHER_VAR"] == "keep-me"


@pytest.mark.parametrize(
    "fixture",
    CLAUDE_FIXTURES,
    ids=[f.name for f in CLAUDE_FIXTURES],
)
@pytest.mark.skipif(
    not all(f.exists() for f in CLAUDE_FIXTURES),
    reason="Task 0 fixtures not captured",
)
def test_golden_fixture_parses_without_raw_fallback(fixture: Path):
    driver = _driver([])
    types: list[str] = []
    for line in fixture.read_text().splitlines():
        if line.strip():
            types.extend(m.type for m in driver.parse_line(line))
    assert any(t == "status" for t in types)  # turn completion / system events
    assert "raw" not in types  # every real line must map to a typed message
    if "assistant" in fixture.read_text():
        assert "assistant_output" in types
