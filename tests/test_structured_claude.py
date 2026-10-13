"""Tests for the Claude Code structured driver."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from drover.server.harness.structured import claude as claude_module
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


def test_default_command_bypasses_permissions():
    command = default_command(binary="/bin/claude")
    assert command[-2:] == ["--permission-mode", "bypassPermissions"]
    # bypass flag must come after the stream-json plumbing flags
    assert "--output-format" in command


def test_resume_command_appends_native_session_without_mutating_source():
    command = ["claude", "-p", "--output-format", "stream-json"]

    resumed = claude_module.resume_command(command, "claude-session-1")

    assert resumed == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--resume",
        "claude-session-1",
    ]
    assert command == ["claude", "-p", "--output-format", "stream-json"]


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


def test_parse_assistant_text_block_preserves_usage_metadata():
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello nexus"}],
                "usage": {"input_tokens": 12, "output_tokens": 3},
                "model": "claude-test",
            },
            "session_id": "session-1",
        }
    )
    message = _driver([]).parse_line(line)[0]
    assert message.payload["usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert message.payload["model"] == "claude-test"
    assert message.payload["native_session_id"] == "session-1"


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


def test_tool_result_payload_carries_tool_name():
    driver = _driver([])
    action_line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    }
                ]
            },
        }
    )
    result_line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}
                ]
            },
        }
    )
    driver.parse_line(action_line)
    [result] = driver.parse_line(result_line)
    assert result.type == "tool_result"
    assert result.payload["tool"] == "Bash"


def test_tool_result_payload_carries_is_error():
    driver = _driver([])
    action_line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "false"},
                    }
                ]
            },
        }
    )
    result_line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "error",
                        "is_error": True,
                    }
                ]
            },
        }
    )
    driver.parse_line(action_line)
    [result] = driver.parse_line(result_line)
    assert result.type == "tool_result"
    assert result.payload["is_error"] is True


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


def test_send_turn_with_images_appends_image_blocks(monkeypatch):
    sent: list[dict] = []
    driver = _driver([])
    monkeypatch.setattr(driver, "send_line", sent.append)
    driver.send_turn(
        "look",
        turn_id="t1",
        images=[{"path": "/tmp/a.png", "media_type": "image/png", "data_b64": "QUJD"}],
    )
    content = sent[0]["message"]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }


def test_send_turn_without_images_unchanged(monkeypatch):
    sent: list[dict] = []
    driver = _driver([])
    monkeypatch.setattr(driver, "send_line", sent.append)
    driver.send_turn("look", turn_id="t1")
    assert sent[0]["message"]["content"] == [{"type": "text", "text": "look"}]


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


def test_block_fanout_preserves_provider_usage_identity():
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "event-1",
            "message": {
                "id": "message-1",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}},
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        }
    )
    messages = _driver([]).parse_line(line)
    assert len(messages) == 2
    assert all(m.payload["usage_message_id"] == "message-1" for m in messages)


def test_default_command_requests_partial_messages():
    assert "--include-partial-messages" in default_command("claude")


def test_recorded_partial_stream_emits_before_completion_without_duplicate_text():
    driver = _driver([])
    outputs = []
    all_messages = []
    for line in (
        (FIXTURES_DIR / "claude_partial_capture.ndjson").read_text().splitlines()
    ):
        obj = json.loads(line)
        if obj.get("type") == "assistant":
            # The completed snapshot must arrive after multiple emitted chunks.
            assert outputs == ["One two three four five six seven", " eight nine ten."]
        messages = driver.parse_line(line)
        all_messages.extend(messages)
        outputs.extend(
            m.text for m in messages if m.payload.get("text_stream_delta") is True
        )
    assert outputs == ["One two three four five six seven", " eight nine ten."]
    finals = [m for m in all_messages if m.payload.get("text_stream_final") is True]
    assert len(finals) == 1
    assert finals[0].text == "".join(outputs)
    assert finals[0].payload["text_stream_id"] == "message-1:0"
    assert finals[0].payload["text_stream_final"] is True
    assert all_messages[-1].payload["turn_complete"] is True
    assert any(m.payload.get("usage_message_id") == "message-1" for m in all_messages)


def test_partial_thinking_preserves_tools_and_resets_for_next_message():
    driver = _driver([])

    def parse(obj):
        return driver.parse_line(json.dumps(obj))

    parse(
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": "m1"}},
        }
    )
    [thinking] = parse(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "Checking"},
            },
        }
    )
    assert thinking.text == "Checking" and thinking.payload["thinking"]
    messages = parse(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [
                    {"type": "thinking", "thinking": "Checking"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"path": "test.py"},
                    },
                    {"type": "text", "text": "Unstreamed block"},
                ],
            },
        }
    )
    assert [m.type for m in messages] == [
        "assistant_output",
        "tool_action",
        "assistant_output",
    ]
    assert messages[-1].text == "Unstreamed block"
    parse(
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": "m2"}},
        }
    )
    [message] = parse(
        {
            "type": "assistant",
            "message": {
                "id": "m2",
                "content": [
                    {"type": "text", "text": "Next answer"},
                ],
            },
        }
    )
    assert message.text == "Next answer"


def test_partial_stream_preserves_final_token_usage():
    from drover.server.harness.usage import session_totals

    driver = _driver([])
    messages = [
        message.to_payload()
        for line in (FIXTURES_DIR / "claude_partial_capture.ndjson")
        .read_text()
        .splitlines()
        for message in driver.parse_line(line)
    ]
    assert session_totals("claude-code", messages).output_tokens == 17


def test_recorded_partial_hub_projection_matches_ios_fixture():
    from drover.server.harness.models import HarnessEvent

    driver = _driver([])
    projected = []
    for line in (
        (FIXTURES_DIR / "claude_partial_capture.ndjson").read_text().splitlines()
    ):
        for message in driver.parse_line(line):
            seq = len(projected) + 1
            event = HarnessEvent(
                event_id=f"e{seq}",
                session_id="fixture-session",
                event_type=message.type,
                seq=seq,
                payload=message.to_payload(),
            )
            row = event.wire_payload()
            row.pop("ts", None)
            projected.append(row)
    fixture = Path(
        "apps/drover/DroverKit/Tests/DroverKitTests/Fixtures/claude-partial-projection.json"
    )
    assert projected == json.loads(fixture.read_text())
    assert [row["type"] for row in projected] == [
        "assistant_output",
        "assistant_output",
        "assistant_output",
        "status",
        "status",
    ]
    assert len({row["event_id"] for row in projected}) == len(projected)
    assert all(
        row["payload"]["text_stream_id"] == "message-1:0" for row in projected[:3]
    )


def test_final_snapshot_identity_survives_a_later_message_start():
    # The old suppression guard only remembered the latest message_start.
    # A delayed snapshot must still finalize its own block, not append a bubble.
    driver = _driver([])
    lines = (FIXTURES_DIR / "claude_partial_capture.ndjson").read_text().splitlines()
    deltas = [m for line in lines[:4] for m in driver.parse_line(line)]
    driver.parse_line(
        json.dumps(
            {
                "type": "stream_event",
                "event": {"type": "message_start", "message": {"id": "message-2"}},
            }
        )
    )
    [final] = driver.parse_line(lines[4])
    assert final.type == "assistant_output"
    assert final.payload["text_stream_final"] is True
    assert final.payload["text_stream_id"] == deltas[0].payload["text_stream_id"]
    assert final.text == "".join(m.text for m in deltas)


def test_recorded_partial_wire_uses_old_ios_assistant_type_with_delta_marker():
    driver = _driver([])
    rows = [
        message.to_payload()
        for line in (FIXTURES_DIR / "claude_partial_capture.ndjson")
        .read_text()
        .splitlines()
        for message in driver.parse_line(line)
    ]
    assistant = [row for row in rows if row["role"] == "assistant"]
    assert [row["type"] for row in assistant] == ["assistant_output"] * 3
    assert [row["payload"].get("text_stream_delta", False) for row in assistant] == [
        True,
        True,
        False,
    ]
    assert assistant[-1]["payload"]["text_stream_final"] is True


def test_thinking_singleton_snapshots_project_matching_block_ids():
    from drover.server.harness.models import HarnessEvent

    driver = _driver([])
    rows = []
    for line in (
        (FIXTURES_DIR / "claude_thinking_partial.ndjson").read_text().splitlines()
    ):
        for message in driver.parse_line(line):
            seq = len(rows) + 1
            row = HarnessEvent(
                event_id=f"e{seq}",
                session_id="fixture-session",
                event_type=message.type,
                seq=seq,
                payload=message.to_payload(),
            ).wire_payload()
            row.pop("ts", None)
            rows.append(row)
    assistant = [r for r in rows if r["type"] == "assistant_output"]
    assert [r["payload"]["text_stream_id"] for r in assistant] == [
        "thinking-message:0",
        "thinking-message:0",
        "thinking-message:1",
        "thinking-message:1",
        "thinking-message:1",
    ]
    assert [r["text"] for r in assistant if r["payload"].get("text_stream_final")] == [
        "Checking the request.",
        "pong",
    ]
    fixture = Path(
        "apps/drover/DroverKit/Tests/DroverKitTests/Fixtures/claude-thinking-projection.json"
    )
    assert rows == json.loads(fixture.read_text())


def test_singleton_snapshots_keep_repeated_text_blocks_and_delayed_message_separate():
    driver = _driver([])

    def parse(obj):
        return driver.parse_line(json.dumps(obj))

    parse(
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": "m1"}},
        }
    )
    for index, kind in enumerate(["thinking", "tool_use", "text", "text"]):
        parse(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": kind},
                },
            }
        )
    parse(
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": "m2"}},
        }
    )
    for expected in [2, 3]:
        [final] = parse(
            {
                "type": "assistant",
                "message": {"id": "m1", "content": [{"type": "text", "text": "same"}]},
            }
        )
        assert final.payload["text_stream_id"] == f"m1:{expected}"
    [next_final] = parse(
        {
            "type": "assistant",
            "message": {"id": "m2", "content": [{"type": "text", "text": "same"}]},
        }
    )
    assert next_final.payload["text_stream_id"] == "m2:0"


def test_stream_block_tracking_is_bounded():
    driver = _driver([])
    for index in range(40):
        driver.parse_line(
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {"type": "message_start", "message": {"id": f"m{index}"}},
                }
            )
        )
        driver.parse_line(
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text"},
                    },
                }
            )
        )
        driver.parse_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": f"m{index}",
                        "content": [{"type": "text", "text": "done"}],
                    },
                }
            )
        )
    assert len(driver._stream_blocks) == 32
    assert len(driver._finalized_blocks) == 32
