"""Tests for the Antigravity CLI (agy) structured driver and provider probe."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from drover.server.harness.structured.agy import AgyDriver, default_command, resume_command
from drover.server.providers.agy import AgyUsageProbe

FAKE_AGY = (
    "import json,sys; args=sys.argv[1:]; "
    'idx=args.index("--print"); prompt=args[idx+1]; '
    'print(json.dumps({"event": "init", "conversation_id": "agy-conv-1"})); '
    'print(json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "echo: " + prompt}})); '
    'print(json.dumps({"event": "result", "result": {"conversation_id": "agy-conv-1", "status": "SUCCESS", "usage": {"input_tokens": 100, "output_tokens": 20}}}))'
)

SLOW_AGY = (
    "import json,sys,time; args=sys.argv[1:]; "
    'idx=args.index("--print"); prompt=args[idx+1]; '
    'time.sleep(1.0); '
    'print(json.dumps({"event": "result", "result": {"status": "SUCCESS"}}))'
)


def _driver(sink: list, native_id: str | None = None) -> AgyDriver:
    return AgyDriver([sys.executable, "-c", FAKE_AGY], None, sink.append, native_session_id=native_id)


def _wait_for(got: list, predicate, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(got):
            return
        time.sleep(0.05)
    raise AssertionError([m.type for m in got])


# -- default_command & resume_command -----------------------------------------


def test_default_command():
    assert default_command("/usr/bin/agy") == ["/usr/bin/agy"]


def test_resume_command():
    cmd = resume_command(["agy"], "conv-123")
    assert cmd == ["agy", "--conversation", "conv-123"]


# -- lifecycle ---------------------------------------------------------------


def test_start_reports_ready():
    got: list = []
    driver = _driver(got)
    driver.start()
    assert got[0].type == "status"
    assert got[0].payload["awaiting"] == "input"
    driver.close()


def test_is_alive_until_close():
    driver = _driver([])
    assert driver.is_alive() is True
    driver.close()
    assert driver.is_alive() is False


# -- turn roundtrip ----------------------------------------------------------


def test_turn_roundtrip_emits_output_then_complete():
    got: list = []
    driver = _driver(got)
    driver.start()
    driver.send_turn("hello", turn_id="t1")
    _wait_for(
        got,
        lambda g: any(m.type == "status" and m.payload.get("turn_complete") for m in g),
    )
    output = next(m for m in got if m.type == "assistant_output")
    assert output.text == "echo: hello"
    assert output.turn_id == "t1"
    assert driver.native_session_id == "agy-conv-1"
    driver.close()


def test_argv_includes_required_flags():
    driver = AgyDriver(["agy"], cwd=None, emit=lambda m: None, native_session_id="conv-456")
    argv = driver._argv_for("hello", model="gemini-3.6-flash")
    assert "--dangerously-skip-permissions" in argv
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--print" in argv and argv[argv.index("--print") + 1] == "hello"
    assert "--model" in argv and argv[argv.index("--model") + 1] == "gemini-3.6-flash"
    assert "--conversation" in argv and argv[argv.index("--conversation") + 1] == "conv-456"


def test_parse_stream_line_tool():
    got: list = []
    driver = _driver(got)
    buf: list[str] = []
    tool_update = {
        "event": "step_update",
        "step_update": {
            "step_index": 3,
            "state": "ACTIVE",
            "step_type": "tool",
            "tool_name": "run_command",
            "tool_info": {"name": "run_command", "parameters": {"CommandLine": "pwd"}},
        },
    }
    messages = driver.parse_stream_line(json.dumps(tool_update), buf, "t1")
    assert len(messages) == 1
    assert messages[0].type == "tool_action"
    assert messages[0].payload["tool"] == "run_command"


def test_answer_permission_raises():
    driver = _driver([])
    with pytest.raises(RuntimeError, match="no interactive approvals"):
        driver.answer_permission("req-1", "allow")


# -- provider probe ----------------------------------------------------------


def test_provider_probe_read():
    probe = AgyUsageProbe()
    snapshot = probe.read(host_id="test-host")
    assert snapshot.provider == "google"
    assert snapshot.host_id == "test-host"
    assert snapshot.status in ("ok", "usage_unavailable")
