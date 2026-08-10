"""Tests for the Antigravity CLI (agy) structured driver and provider probe."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from drover.server.harness.structured.agy import (
    AgyDriver,
    default_command,
    resume_command,
)
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
    "time.sleep(1.0); "
    'print(json.dumps({"event": "result", "result": {"status": "SUCCESS"}}))'
)


def _driver(sink: list, native_id: str | None = None) -> AgyDriver:
    return AgyDriver(
        [sys.executable, "-c", FAKE_AGY], None, sink.append, native_session_id=native_id
    )


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
    driver = AgyDriver(
        ["agy"], cwd=None, emit=lambda m: None, native_session_id="conv-456"
    )
    argv = driver._argv_for("hello", model="gemini-3.6-flash-high")
    assert "--dangerously-skip-permissions" in argv
    assert (
        "--output-format" in argv
        and argv[argv.index("--output-format") + 1] == "stream-json"
    )
    assert "--print" in argv and argv[argv.index("--print") + 1] == "hello"
    assert (
        "--model" in argv and argv[argv.index("--model") + 1] == "gemini-3.6-flash-high"
    )
    assert (
        "--conversation" in argv
        and argv[argv.index("--conversation") + 1] == "conv-456"
    )


def test_argv_scopes_workspace_to_cwd():
    # agy does not take its workspace from the process cwd -- without an
    # explicit --add-dir it runs in ~/.gemini/antigravity-cli/scratch no
    # matter what we hand Popen. Every turn is a fresh process, so the flag
    # has to be on every argv, not just the first.
    driver = AgyDriver(["agy"], cwd="/Volumes/M2 1/drover", emit=lambda m: None)
    argv = driver._argv_for("hello")
    assert "--add-dir" in argv
    assert argv[argv.index("--add-dir") + 1] == "/Volumes/M2 1/drover"


def test_argv_omits_add_dir_without_cwd():
    driver = AgyDriver(["agy"], cwd=None, emit=lambda m: None)
    assert "--add-dir" not in driver._argv_for("hello")


def test_argv_does_not_duplicate_caller_supplied_add_dir():
    driver = AgyDriver(
        ["agy", "--add-dir", "/srv/repo"], cwd="/tmp/other", emit=lambda m: None
    )
    argv = driver._argv_for("hello")
    assert argv.count("--add-dir") == 1
    assert argv[argv.index("--add-dir") + 1] == "/srv/repo"


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


@pytest.mark.parametrize(
    "line",
    [
        '{"event": "step_update", "step_update": {"step_type": "tool", '
        '"state": "ACTIVE", "tool_info": null}}',
        '{"event": "init", "init": null}',
        '{"event": "step_update", "step_update": null}',
    ],
)
def test_parse_stream_line_survives_null_sub_objects(line):
    """A null sub-object must not kill the turn thread.

    ``parse_stream_line`` runs on the pump thread, where an escaping
    exception ends the turn with no completion event at all -- the session
    just hangs.
    """
    driver = _driver([])

    driver.parse_stream_line(line, [], "t1")


def test_answer_permission_raises():
    driver = _driver([])
    with pytest.raises(RuntimeError, match="no interactive approvals"):
        driver.answer_permission("req-1", "allow")


def test_interrupt_terminates_an_in_flight_turn():
    got: list = []
    driver = AgyDriver([sys.executable, "-c", SLOW_AGY], None, got.append)
    driver.send_turn("hello", turn_id="t1")
    _wait_for(got, lambda _g: driver._turn_process is not None, timeout=5.0)
    driver.interrupt()
    _wait_for(got, lambda g: any(m.type == "error" for m in g))
    driver.close()
    assert driver.is_alive() is False


# -- provider probe ----------------------------------------------------------
#
# Every credential source is injected. Reading the real ``~/.gemini`` would
# make these pass or fail on whether this machine happens to be signed into
# agy, which is how a "hermetic" suite starts passing for the wrong reason.


def test_provider_probe_reports_the_signed_in_account(tmp_path: Path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(json.dumps({"active": "someone@example.com", "old": []}))

    snapshot = AgyUsageProbe(accounts_path=accounts).read(host_id="test-host")

    assert snapshot.provider == "google"
    assert snapshot.host_id == "test-host"
    assert snapshot.account_label == "someone@example.com"


def test_provider_probe_says_capacity_is_unavailable_rather_than_ok(tmp_path: Path):
    """No quota source exists yet, so the card must not claim to be healthy."""
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(json.dumps({"active": "someone@example.com"}))

    snapshot = AgyUsageProbe(accounts_path=accounts).read()

    assert snapshot.status == "usage_unavailable"
    assert snapshot.error_category == "quota_api_unreachable"
    assert snapshot.windows == ()


def test_provider_probe_falls_back_to_a_generic_label(tmp_path: Path):
    snapshot = AgyUsageProbe(accounts_path=tmp_path / "missing.json").read()

    assert snapshot.account_label == "Antigravity"
    assert snapshot.status == "usage_unavailable"


def test_provider_probe_never_raises_on_a_broken_accounts_file(tmp_path: Path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text("{ not json")

    snapshot = AgyUsageProbe(accounts_path=accounts).read()

    assert snapshot.account_label == "Antigravity"
