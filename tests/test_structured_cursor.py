from __future__ import annotations

import json
import sys
import time

from drover.server.harness.structured.cursor import CursorDriver


def _wait_for(items, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(items):
            return
        time.sleep(0.01)
    raise AssertionError(f"condition not met: {items!r}")


def test_argv_uses_reviewed_streaming_resume_and_no_permission_escalation():
    driver = CursorDriver(["cursor-agent"], "/project", lambda _: None, "chat-1")

    argv = driver._argv_for("hello", model="grok-4.7")

    assert argv == [
        "cursor-agent",
        "--resume",
        "chat-1",
        "--model",
        "grok-4.7",
        "--print",
        "--output-format",
        "stream-json",
        "--stream-partial-output",
        "hello",
    ]
    assert not {"--force", "--yolo", "--trust", "--sandbox"} & set(argv)


def test_argv_appends_to_login_shell_command_not_shell_positionals():
    driver = CursorDriver(
        ["/bin/zsh", "-lc", "exec /Users/arnab/.local/bin/cursor-agent"],
        None,
        lambda _: None,
    )

    argv = driver._argv_for("prompt with spaces")

    assert argv[:2] == ["/bin/zsh", "-lc"]
    assert argv[2].startswith("exec /Users/arnab/.local/bin/cursor-agent ")
    assert "--stream-partial-output" in argv[2]
    assert "'prompt with spaces'" in argv[2]


def test_parse_documented_stream_json_deltas_tools_and_terminal_result():
    driver = CursorDriver(["cursor-agent"], None, lambda _: None)
    init = driver.parse_stream_line(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "chat-7",
                "model": "grok-4.7",
            }
        ),
        "turn-1",
    )
    delta = driver.parse_stream_line(
        json.dumps(
            {
                "type": "assistant",
                "timestamp_ms": 1,
                "message": {"content": [{"type": "text", "text": "Hello"}]},
                "session_id": "chat-7",
            }
        ),
        "turn-1",
    )
    duplicate = driver.parse_stream_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello"}]},
                "session_id": "chat-7",
            }
        ),
        "turn-1",
    )
    action = driver.parse_stream_line(
        json.dumps(
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": "call-1",
                "tool_call": {"readToolCall": {"args": {"path": "file.txt"}}},
            }
        ),
        "turn-1",
    )
    result = driver.parse_stream_line(
        json.dumps(
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": "call-1",
                "tool_call": {
                    "readToolCall": {"result": {"success": {"content": "ok"}}}
                },
            }
        ),
        "turn-1",
    )
    terminal = driver.parse_stream_line(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "chat-7",
                "duration_ms": 12,
            }
        ),
        "turn-1",
    )

    assert init[0].payload["native_session_id"] == "chat-7"
    assert delta[0].text == "Hello"
    assert duplicate == []
    assert action[0].payload == {
        "tool": "readToolCall",
        "tool_use_id": "call-1",
        "input": {"path": "file.txt"},
    }
    assert result[0].payload["status"] == "success"
    assert terminal[0].payload["turn_complete"] is True
    assert terminal[0].payload["native_session_id"] == "chat-7"


def test_parse_malformed_and_auth_error_output_is_safe():
    driver = CursorDriver(["cursor-agent"], None, lambda _: None)

    raw = driver.parse_stream_line("not-json", "turn-1")
    error = driver.parse_error(
        1, "Authentication required: token super-secret", "turn-1"
    )

    assert raw[0].type == "raw"
    assert error.type == "error"
    assert "super-secret" not in error.text
    assert "<redacted>" in error.text


def test_nonzero_turn_emits_error_not_completion():
    got = []
    driver = CursorDriver(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('Authentication required: token hidden\\n'); raise SystemExit(1)",
        ],
        None,
        got.append,
    )

    driver.send_turn("hello", "turn-1")
    _wait_for(got, lambda rows: any(row.type == "error" for row in rows))
    driver.close()

    assert not any(row.payload.get("turn_complete") for row in got)
    assert "hidden" not in next(row.text for row in got if row.type == "error")
