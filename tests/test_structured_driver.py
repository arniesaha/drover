"""Tests for the structured driver base: message schema + subprocess pump."""

from __future__ import annotations

import json
import sys
import time

from drover.server.harness.structured.driver import (
    MESSAGE_TYPES,
    ProcessDriver,
    StructuredMessage,
)


def test_message_defaults_and_payload_roundtrip():
    message = StructuredMessage(type="assistant_output", role="assistant", text="hi")
    assert message.event_id.startswith("harness-event-")
    data = message.to_payload()
    assert data["type"] == "assistant_output"
    assert data["text"] == "hi"
    assert json.dumps(data)  # JSON-safe


def test_vocabulary_is_exact():
    assert MESSAGE_TYPES == {
        "assistant_output",
        "user_input",
        "tool_action",
        "tool_result",
        "approval_prompt",
        "approval_response",
        "status",
        "error",
        "raw",
    }


class _EchoDriver(ProcessDriver):
    def parse_line(self, line: str):
        obj = json.loads(line)
        return [
            StructuredMessage(
                type="assistant_output", role="assistant", text=obj["text"]
            )
        ]


def _collect_driver(script: str):
    got: list[StructuredMessage] = []
    driver = _EchoDriver([sys.executable, "-c", script], None, got.append)
    driver.start()
    return driver, got


def _wait_for(got, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(got):
            return
        time.sleep(0.05)
    raise AssertionError(f"condition not met; got {[m.type for m in got]}")


def test_pump_parses_and_falls_back_to_raw():
    script = 'print(\'{"text": "one"}\'); print("not json"); import sys; sys.exit(0)'
    driver, got = _collect_driver(script)
    _wait_for(got, lambda g: any(m.type == "status" for m in g))
    types = [m.type for m in got]
    assert "assistant_output" in types
    assert "raw" in types
    raw = next(m for m in got if m.type == "raw")
    assert raw.text == "not json"
    driver.close()


def test_nonzero_exit_emits_error_with_stderr_tail():
    # ~50 stderr lines before exiting nonzero: the LAST line must appear in
    # the error text, which requires the stderr pump to be fully drained
    # before on_exit() reads the tail.
    script = (
        "import sys\n"
        "for i in range(49):\n"
        '    print(f"noise {i}", file=sys.stderr)\n'
        'print("boom final", file=sys.stderr)\n'
        "sys.exit(3)\n"
    )
    driver, got = _collect_driver(script)
    _wait_for(got, lambda g: any(m.type == "error" for m in g))
    error = next(m for m in got if m.type == "error")
    assert "boom final" in error.text
    status = next(m for m in got if m.type == "status")
    assert status.payload["exited"] == 3
    driver.close()
