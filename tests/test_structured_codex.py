"""Tests for the Codex structured driver (per-turn respawn).

Controller adjustments to Task 4 (authoritative over the original brief,
see .superpowers/sdd/task-4-brief.md's header note and FINDINGS.md sec 2):
Codex has no `proto` subcommand and no persistent bidirectional process, so
CodexDriver is NOT a ProcessDriver subclass -- it respawns `codex exec`
once per turn, mirroring the plan's Gemini driver shape.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from drover.server.harness.structured.codex import CodexDriver, default_command

FIXTURES_DIR = Path("tests/fixtures/structured")
CODEX_FIXTURES = [
    FIXTURES_DIR / "codex_basic.ndjson",
    FIXTURES_DIR / "codex_resume.ndjson",
]

# Fake `codex` binary: logs its own argv (JSON, one line per invocation) to
# the file named by $CODEX_ARGV_LOG, then emits the real observed NDJSON
# vocabulary (FINDINGS.md sec 2) for a clean single-turn run.
FAKE_CODEX = """
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["CODEX_ARGV_LOG"], "a") as fh:
    print(json.dumps(argv), file=fh)
events = [
    {"type": "thread.started", "thread_id": "thread-abc"},
    {"type": "turn.started"},
    {"type": "item.completed",
     "item": {"id": "item_0", "type": "agent_message", "text": "hi"}},
    {"type": "turn.completed", "usage": {"input_tokens": 1}},
]
for event in events:
    print(json.dumps(event), flush=True)
"""

# Fake binary that sleeps before finishing, so a second send_turn can be
# attempted while the first is still in flight. Logs its argv (to
# $CODEX_ARGV_LOG, if set) and pid (to $CODEX_PID_FILE, if set) so tests
# can count spawns and check subprocess liveness after close().
SLOW_CODEX = """
import json, os, sys, time
log = os.environ.get("CODEX_ARGV_LOG")
if log:
    with open(log, "a") as fh:
        print(json.dumps(sys.argv[1:]), file=fh)
pid_file = os.environ.get("CODEX_PID_FILE")
if pid_file:
    with open(pid_file, "w") as fh:
        print(os.getpid(), file=fh)
time.sleep(1.0)
print(json.dumps({"type": "turn.completed", "usage": {}}), flush=True)
"""

FAIL_CODEX = "import sys; sys.exit(3)"


def _driver(sink: list) -> CodexDriver:
    return CodexDriver(["true"], None, sink.append)


def _wait_for(got: list, predicate, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(got):
            return
        time.sleep(0.05)
    raise AssertionError([m.type for m in got])


# -- default_command --------------------------------------------------------


def test_default_command_uses_binary():
    assert default_command("/opt/bin/codex") == ["/opt/bin/codex"]


def test_default_command_falls_back_to_which(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert default_command(None) == ["codex"]


# -- parse_line, literal lines from FINDINGS.md / the golden fixtures -------


def test_parse_thread_started_sets_native_session_id():
    line = (
        '{"type":"thread.started",'
        '"thread_id":"019f38e0-c232-7233-85d0-980957a5f7f1"}'
    )
    message = _driver([]).parse_line(line)[0]
    assert message.type == "status"
    assert (
        message.payload["native_session_id"] == "019f38e0-c232-7233-85d0-980957a5f7f1"
    )


def test_parse_turn_started_is_status():
    message = _driver([]).parse_line(json.dumps({"type": "turn.started"}))[0]
    assert message.type == "status"


def test_parse_agent_message_item_completed():
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": "hello nexus"},
        }
    )
    message = _driver([]).parse_line(line)[0]
    assert message.type == "assistant_output"
    assert message.text == "hello nexus"


def test_parse_turn_completed_marks_awaiting_input():
    line = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 18240,
                "cached_input_tokens": 4992,
                "output_tokens": 67,
                "reasoning_output_tokens": 59,
            },
        }
    )
    message = _driver([]).parse_line(line)[0]
    assert message.type == "status"
    assert message.payload["turn_complete"] is True
    assert message.payload["awaiting"] == "input"
    assert message.payload["usage"]["output_tokens"] == 67


def test_parse_command_execution_started_and_completed():
    begin = json.dumps(
        {
            "type": "item.started",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": '/bin/zsh -lc "echo hi"',
                "aggregated_output": "",
                "exit_code": None,
                "status": "in_progress",
            },
        }
    )
    end = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": '/bin/zsh -lc "echo hi"',
                "aggregated_output": "hi\n",
                "exit_code": 0,
                "status": "completed",
            },
        }
    )
    driver = _driver([])
    action = driver.parse_line(begin)[0]
    result = driver.parse_line(end)[0]
    assert action.type == "tool_action"
    assert action.text == '/bin/zsh -lc "echo hi"'
    assert result.type == "tool_result"
    assert result.text == "hi\n"
    assert result.payload["exit_code"] == 0
    assert result.payload["status"] == "completed"


def test_parse_non_json_line_degrades_to_raw():
    message = _driver([]).parse_line("not json at all")[0]
    assert message.type == "raw"
    assert message.payload["stream"] == "stdout"


def _messages_for_line(line: str) -> list:
    driver = CodexDriver(["codex"], cwd=None, emit=lambda m: None)
    return driver.parse_line(json.dumps(line) if isinstance(line, dict) else line)


def test_command_item_started_payload_has_tool_keys():
    [msg] = _messages_for_line(
        {
            "type": "item.started",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "pytest -x",
                "status": "in_progress",
            },
        }
    )
    assert msg.type == "tool_action"
    assert msg.payload["tool"] == "shell"
    assert msg.payload["tool_use_id"] == "item_1"
    assert msg.payload["input"] == {"command": "pytest -x"}


def test_command_item_completed_payload_has_tool_keys():
    [msg] = _messages_for_line(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "pytest -x",
                "aggregated_output": "3 passed\n",
                "exit_code": 0,
                "status": "completed",
            },
        }
    )
    assert msg.type == "tool_result"
    assert msg.payload["tool"] == "shell"
    assert msg.payload["tool_use_id"] == "item_1"
    assert msg.payload["exit_code"] == 0
    assert msg.text == "3 passed\n"


def test_reasoning_item_maps_to_thinking():
    [msg] = _messages_for_line(
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "reasoning", "text": "let me look"},
        }
    )
    assert msg.type == "assistant_output"
    assert msg.payload["thinking"] is True
    assert msg.text == "let me look"


# -- answer_permission: no approval channel ----------------------------------


def test_answer_permission_raises():
    driver = _driver([])
    with pytest.raises(RuntimeError, match="approval channel"):
        driver.answer_permission("req-1", "allow")


# -- turn lifecycle: per-turn respawn, argv shape, resume + thread id --------


def test_first_turn_argv_has_exec_not_resume(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("CODEX_ARGV_LOG", str(log))
    got: list = []
    driver = CodexDriver([sys.executable, "-c", FAKE_CODEX], None, got.append)
    driver.start()
    driver.send_turn(
        "do it",
        turn_id="t1",
        model="gpt-5.6-sol",
        thinking_effort="high",
    )
    _wait_for(
        got,
        lambda g: any(m.type == "status" and m.payload.get("turn_complete") for m in g),
    )
    argv1 = json.loads(log.read_text().splitlines()[0])
    assert "exec" in argv1
    assert "resume" not in argv1
    assert argv1[-1] == "do it"
    assert argv1.count("--model") == 1
    assert argv1[argv1.index("--model") + 1] == "gpt-5.6-sol"
    assert argv1.count('model_reasoning_effort="high"') == 1
    sandbox_flag = argv1.index("--sandbox")
    assert argv1[sandbox_flag + 1] == "danger-full-access"
    driver.close()


def test_second_turn_argv_has_resume_and_captured_thread_id(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("CODEX_ARGV_LOG", str(log))
    got: list = []
    driver = CodexDriver([sys.executable, "-c", FAKE_CODEX], None, got.append)
    driver.start()
    driver.send_turn(
        "first",
        turn_id="t1",
        model="gpt-5.6-sol",
        thinking_effort="high",
    )
    _wait_for(
        got,
        lambda g: any(m.type == "status" and m.payload.get("turn_complete") for m in g),
    )
    driver.send_turn(
        "second",
        turn_id="t2",
        model="gpt-5.6-sol",
        thinking_effort="high",
    )
    _wait_for(
        got,
        lambda g: sum(
            1 for m in g if m.type == "status" and m.payload.get("turn_complete")
        )
        >= 2,
    )
    argv2 = json.loads(log.read_text().splitlines()[1])
    assert "resume" in argv2
    assert "thread-abc" in argv2
    assert argv2[-1] == "second"
    assert argv2.count("--model") == 1
    assert argv2[argv2.index("--model") + 1] == "gpt-5.6-sol"
    assert argv2.count('model_reasoning_effort="high"') == 1
    # ``codex exec resume`` rejects ``--sandbox``; full access is requested via
    # the ``-c`` config override instead. Asserting the *absence* of the flag
    # keeps the regression (every follow-up turn dying at arg-parse) locked out.
    assert "--sandbox" not in argv2
    assert argv2.count("sandbox_mode=danger-full-access") == 1
    driver.close()


def test_send_turn_while_in_flight_raises():
    got: list = []
    driver = CodexDriver([sys.executable, "-c", SLOW_CODEX], None, got.append)
    driver.start()
    driver.send_turn("first", turn_id="t1")
    with pytest.raises(RuntimeError, match="turn already in flight"):
        driver.send_turn("second", turn_id="t2")
    driver.close()


def test_back_to_back_send_turns_spawn_exactly_one_subprocess(tmp_path, monkeypatch):
    # No sleeps between the two calls: the in-flight flag is set inside the
    # lock before send_turn returns, so this is deterministic (a
    # Thread.is_alive()-based check would be racy here -- a created but
    # not-yet-started worker reports is_alive() == False).
    log = tmp_path / "argv.log"
    monkeypatch.setenv("CODEX_ARGV_LOG", str(log))
    got: list = []
    driver = CodexDriver([sys.executable, "-c", SLOW_CODEX], None, got.append)
    driver.start()
    driver.send_turn("first", turn_id="t1")
    with pytest.raises(RuntimeError, match="turn already in flight"):
        driver.send_turn("second", turn_id="t2")
    _wait_for(
        got,
        lambda g: any(m.type == "status" and m.payload.get("turn_complete") for m in g),
    )
    assert len(log.read_text().splitlines()) == 1
    driver.close()


def test_close_during_turn_kills_subprocess(tmp_path, monkeypatch):
    pid_file = tmp_path / "codex.pid"
    monkeypatch.setenv("CODEX_PID_FILE", str(pid_file))
    got: list = []
    driver = CodexDriver([sys.executable, "-c", SLOW_CODEX], None, got.append)
    driver.start()
    driver.send_turn("first", turn_id="t1")
    deadline = time.time() + 10.0
    while not pid_file.exists() and time.time() < deadline:
        time.sleep(0.05)
    pid = int(pid_file.read_text())
    driver.close()
    # close() must not return with the turn subprocess still running.
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert driver.is_alive() is False


def test_interrupt_kills_in_flight_turn():
    got: list = []
    driver = CodexDriver([sys.executable, "-c", SLOW_CODEX], None, got.append)
    driver.start()
    driver.send_turn("first", turn_id="t1")
    time.sleep(0.1)
    driver.interrupt()
    _wait_for(
        got,
        lambda g: any(
            m.type == "status" and m.payload.get("exited") is not None for m in g
        ),
    )
    exited = next(m for m in got if m.type == "status" and "exited" in m.payload)
    assert exited.payload["exited"] != 0
    driver.close()


def test_nonzero_exit_emits_error():
    got: list = []
    driver = CodexDriver([sys.executable, "-c", FAIL_CODEX], None, got.append)
    driver.start()
    driver.send_turn("hi", turn_id="t1")
    _wait_for(got, lambda g: any(m.type == "error" for m in g))
    driver.close()


def test_is_alive_until_close():
    driver = _driver([])
    assert driver.is_alive() is True
    driver.close()
    assert driver.is_alive() is False


# -- golden fixtures: both codex_basic.ndjson and codex_resume.ndjson -------


@pytest.mark.parametrize("fixture", CODEX_FIXTURES)
def test_golden_fixture_parses_without_raw_fallback(fixture):
    if not fixture.exists():
        pytest.skip("Task 0 fixture not captured")
    driver = _driver([])
    types: list[str] = []
    for line in fixture.read_text().splitlines():
        if line.strip():
            types.extend(m.type for m in driver.parse_line(line))
    assert "assistant_output" in types
    assert "raw" not in types
