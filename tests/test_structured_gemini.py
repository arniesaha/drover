"""Tests for the Gemini structured driver (per-turn respawn).

Controller adjustments to Task 5 (authoritative over the original brief, see
.superpowers/sdd/task-5-brief.md and FINDINGS.md sec 3): gemini is
UNAUTHENTICATED on this host, so there is no success-path capture to test
against a golden fixture -- these tests exercise fake-CLI stand-ins only.
The one thing that WAS captured live is the error envelope on stderr
(gemini_basic.json), so that shape gets a dedicated parser test using the
exact captured bytes.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from drover.server.harness.structured.gemini import GeminiDriver, default_command

FIXTURES_DIR = Path("tests/fixtures/structured")
GEMINI_ERROR_ENVELOPE = FIXTURES_DIR / "gemini_basic.json"

# Emits stream-json NDJSON: one "message"/assistant event, then a "result"
# event (the turn-complete signal the new parser looks for).
FAKE_GEMINI = (
    "import json,sys; args=sys.argv[1:]; "
    'idx=args.index("-p"); prompt=args[idx+1]; '
    'print(json.dumps({"type": "message", "role": "assistant", '
    '"content": "echo: " + prompt})); '
    'print(json.dumps({"type": "result", "status": "success", "stats": {}}))'
)

# Logs its own argv (JSON, one line) to $GEMINI_ARGV_LOG before emitting the
# same NDJSON shape as FAKE_GEMINI, so argv-shape tests (the --approval-mode
# yolo / -o stream-json flags in particular) don't need a separate script.
FAKE_GEMINI_LOGGING = """
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("GEMINI_ARGV_LOG")
if log:
    with open(log, "a") as fh:
        print(json.dumps(args), file=fh)
idx = args.index("-p")
prompt = args[idx + 1]
print(json.dumps({"type": "message", "role": "assistant", "content": "echo: " + prompt}))
print(json.dumps({"type": "result", "status": "success", "stats": {}}))
"""

# Sleeps before finishing so a second send_turn / close() can be attempted
# while the first is still in flight. Logs argv and pid so tests can check
# subprocess liveness after close().
SLOW_GEMINI = """
import json, os, sys, time
log = os.environ.get("GEMINI_ARGV_LOG")
if log:
    with open(log, "a") as fh:
        print(json.dumps(sys.argv[1:]), file=fh)
pid_file = os.environ.get("GEMINI_PID_FILE")
if pid_file:
    with open(pid_file, "w") as fh:
        print(os.getpid(), file=fh)
time.sleep(1.0)
print(json.dumps({"type": "message", "role": "assistant", "content": "late"}))
print(json.dumps({"type": "result", "status": "success", "stats": {}}))
"""

FAIL_GEMINI_SILENT = "import sys; sys.exit(2)"

# Fake CLI that emits the golden NDJSON fixture (tests/fixtures/structured/
# gemini_stream.ndjson, captured live 2026-08-04) verbatim to stdout, so the
# mapping test exercises the real captured line shapes rather than
# hand-rolled ones. Also logs argv, mirroring FAKE_GEMINI_LOGGING.
FAKE_GEMINI_STREAM = """
import json, os, sys
argv = sys.argv[1:]
log = os.environ.get("GEMINI_ARGV_LOG")
if log:
    with open(log, "a") as fh:
        print(json.dumps(argv), file=fh)
for line in open(os.environ["GEMINI_STREAM_FIXTURE"]):
    if line.strip():
        print(line.rstrip(), flush=True)
"""

FAKE_GEMINI_NO_RESULT = """
import json
print(json.dumps({"type": "init", "session_id": "gemini-native-1"}), flush=True)
print(json.dumps({"type": "message", "role": "assistant", "content": "partial"}), flush=True)
"""


def _driver(sink: list) -> GeminiDriver:
    return GeminiDriver([sys.executable, "-c", FAKE_GEMINI], None, sink.append)


def _wait_for(got: list, predicate, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(got):
            return
        time.sleep(0.05)
    raise AssertionError([m.type for m in got])


def _run_fake_turn(tmp_path: Path, source: str) -> list:
    """Spawn a fake gemini CLI from `source`, run one turn, return messages.

    Mirrors the driver-construction + send_turn + wait-for-terminal-message
    pattern every other test in this file hand-rolls, so the new mapping
    tests don't need their own bespoke plumbing.
    """
    del tmp_path  # kept for parity with the brief's helper signature
    got: list = []
    driver = GeminiDriver([sys.executable, "-c", source], None, got.append)
    driver.start()
    got.clear()  # drop the start()-emitted "ready" status; only turn output
    driver.send_turn("hello", turn_id="t1")
    _wait_for(
        got,
        lambda g: any(
            (m.type == "status" and m.payload.get("turn_complete")) or m.type == "error"
            for m in g
        ),
    )
    driver.close()
    return got


# -- default_command ---------------------------------------------------------


def test_default_command_uses_binary():
    assert default_command("/opt/bin/gemini") == ["/opt/bin/gemini"]


def test_default_command_falls_back_to_which(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert default_command(None) == ["gemini"]


# -- start / lifecycle --------------------------------------------------------


def test_start_reports_ready():
    got = []
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


# -- turn roundtrip ------------------------------------------------------------


def test_turn_roundtrip_emits_output_then_complete():
    got = []
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
    driver.close()


def test_failed_turn_emits_error():
    got = []
    driver = GeminiDriver([sys.executable, "-c", FAIL_GEMINI_SILENT], None, got.append)
    driver.start()
    driver.send_turn("hello", turn_id="t1")
    _wait_for(got, lambda g: any(m.type == "error" for m in g))
    driver.close()


def test_argv_includes_approval_mode_yolo(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("GEMINI_ARGV_LOG", str(log))
    got: list = []
    driver = GeminiDriver([sys.executable, "-c", FAKE_GEMINI_LOGGING], None, got.append)
    driver.start()
    driver.send_turn("hello", turn_id="t1", model="gemini-2.5-pro")
    _wait_for(
        got,
        lambda g: any(m.type == "status" and m.payload.get("turn_complete") for m in g),
    )
    argv = json.loads(log.read_text().splitlines()[0])
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "hello"
    assert "-o" in argv and argv[argv.index("-o") + 1] == "stream-json"
    assert "--approval-mode" in argv
    assert argv[argv.index("--approval-mode") + 1] == "yolo"
    assert "--skip-trust" in argv
    assert "--resume" not in argv
    assert argv.count("--model") == 1
    assert argv[argv.index("--model") + 1] == "gemini-2.5-pro"
    driver.close()


def test_argv_uses_stream_json(tmp_path):
    del tmp_path
    driver = GeminiDriver(["gemini"], cwd=None, emit=lambda m: None)
    argv = driver._argv_for("hello")
    assert "-o" in argv and argv[argv.index("-o") + 1] == "stream-json"
    assert "--approval-mode" in argv and "yolo" in argv
    assert "--skip-trust" in argv


# -- golden fixture: live-captured NDJSON stream (gemini 0.46.0, Task 1) ------


def test_stream_json_turn_maps_events(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "GEMINI_STREAM_FIXTURE", str(FIXTURES_DIR / "gemini_stream.ndjson")
    )
    messages = _run_fake_turn(tmp_path, FAKE_GEMINI_STREAM)
    types = [m.type for m in messages]
    # init status, tool_action, tool_result, ONE coalesced assistant_output,
    # then turn-complete status. The user-echo message line is skipped
    # (manager already records user_input for every sent turn).
    assert types == [
        "status",
        "tool_action",
        "tool_result",
        "assistant_output",
        "status",
    ]
    action = messages[1]
    assert action.payload["tool"] == "list_directory"
    assert action.payload["tool_use_id"] == "list_directory__859y31fx"
    assert action.payload["input"] == {"dir_path": "."}
    result = messages[2]
    assert result.payload["tool_use_id"] == "list_directory__859y31fx"
    output = messages[3]
    assert output.text.startswith("probe-ok")
    assert "current directory" in output.text  # both delta chunks joined
    final = messages[4]
    assert final.payload["turn_complete"] is True
    assert final.payload["awaiting"] == "input"


def test_zero_exit_without_result_still_marks_turn_complete(tmp_path):
    messages = _run_fake_turn(tmp_path, FAKE_GEMINI_NO_RESULT)
    assert [m.type for m in messages] == ["status", "assistant_output", "status"]
    assert messages[1].text == "partial"
    final = messages[-1]
    assert final.payload["turn_complete"] is True
    assert final.payload["awaiting"] == "input"
    assert final.payload["missing_result"] is True


# -- answer_permission: no interactive approvals (yolo mode) ------------------


def test_answer_permission_raises():
    driver = _driver([])
    with pytest.raises(RuntimeError, match="no interactive approvals"):
        driver.answer_permission("req-1", "allow")


# -- error envelope parsing, exact captured shape from FINDINGS.md sec 3 -----


def test_parse_error_envelope_matches_live_capture():
    stderr_text = GEMINI_ERROR_ENVELOPE.read_text()
    driver = _driver([])
    message = driver.parse_error(41, stderr_text, turn_id="t1")
    assert message.type == "error"
    assert message.turn_id == "t1"
    assert message.text == (
        "When using Gemini API, you must specify the GEMINI_API_KEY "
        "environment variable.\nUpdate your environment and try again "
        "(no reload needed if using .env)!"
    )
    assert message.payload["code"] == 41
    assert message.payload["session_id"] == "39f02d66-e12d-4f27-9cd7-4ccd68716220"


def test_parse_error_envelope_falls_back_when_not_json():
    driver = _driver([])
    message = driver.parse_error(1, "not json at all", turn_id="t1")
    assert message.type == "error"
    assert message.text == "not json at all"
    assert message.payload["returncode"] == 1


def test_parse_error_envelope_falls_back_when_missing_error_key():
    driver = _driver([])
    message = driver.parse_error(1, json.dumps({"foo": "bar"}), turn_id="t1")
    assert message.type == "error"
    assert message.text == json.dumps({"foo": "bar"})


def test_live_error_envelope_end_to_end():
    # Same envelope, but exercised through a real subprocess writing to
    # stderr and exiting nonzero, matching the live gemini_basic.json
    # capture (session_id + error.{type,message,code} on stderr, empty
    # stdout, exit code 41).
    envelope = GEMINI_ERROR_ENVELOPE.read_text()
    script = "import sys; sys.stderr.write(" + repr(envelope) + "); sys.exit(41)"
    got: list = []
    driver = GeminiDriver([sys.executable, "-c", script], None, got.append)
    driver.start()
    driver.send_turn("hello", turn_id="t1")
    _wait_for(got, lambda g: any(m.type == "error" for m in g))
    error = next(m for m in got if m.type == "error")
    assert "GEMINI_API_KEY" in error.text
    assert error.payload["code"] == 41
    driver.close()


# -- one-in-flight discipline --------------------------------------------------


def test_send_turn_while_in_flight_raises():
    got: list = []
    driver = GeminiDriver([sys.executable, "-c", SLOW_GEMINI], None, got.append)
    driver.start()
    driver.send_turn("first", turn_id="t1")
    with pytest.raises(RuntimeError, match="turn already in flight"):
        driver.send_turn("second", turn_id="t2")
    driver.close()


def test_back_to_back_send_turns_spawn_exactly_one_subprocess(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("GEMINI_ARGV_LOG", str(log))
    got: list = []
    driver = GeminiDriver([sys.executable, "-c", SLOW_GEMINI], None, got.append)
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


# -- close() during an in-flight turn kills the subprocess --------------------


def test_close_during_turn_kills_subprocess(tmp_path, monkeypatch):
    pid_file = tmp_path / "gemini.pid"
    monkeypatch.setenv("GEMINI_PID_FILE", str(pid_file))
    got: list = []
    driver = GeminiDriver([sys.executable, "-c", SLOW_GEMINI], None, got.append)
    driver.start()
    driver.send_turn("first", turn_id="t1")
    deadline = time.time() + 10.0
    while not pid_file.exists() and time.time() < deadline:
        time.sleep(0.05)
    pid = int(pid_file.read_text())
    driver.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert driver.is_alive() is False


def test_interrupt_kills_in_flight_turn():
    got: list = []
    driver = GeminiDriver([sys.executable, "-c", SLOW_GEMINI], None, got.append)
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


# -- send_turn after close() --------------------------------------------------


def test_send_turn_after_close_raises():
    driver = _driver([])
    driver.start()
    driver.close()
    with pytest.raises(RuntimeError, match="driver is closed"):
        driver.send_turn("hello", turn_id="t1")
