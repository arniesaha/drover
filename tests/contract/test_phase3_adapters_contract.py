"""Phase 3 adapter wire contracts, unchanged for Python and Rust harnessd."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

import pytest

from drover.server.harness.identity import harness_event_identity

from .support import wait_until
from .test_harnessd_contract import request


def _events(fake_hub, session_id):
    return [
        event
        for item in fake_hub.requests
        if item["path"] == "/harness/events"
        for event in item["body"]["events"]
        if event["session_id"] == session_id
    ]


def _event(events, event_type, *, text=None):
    return next(
        event
        for event in events
        if event["type"] == event_type and (text is None or event["text"] == text)
    )


def _assert_d10_projection(event, expected_payload):
    """Assert the visible wire body and its real cross-daemon D10 identity."""
    assert event["payload"] == expected_payload
    expected = harness_event_identity(
        session_id=event["session_id"],
        seq=event["seq"],
        event_type=event["type"],
        created_at=datetime.fromisoformat(event["ts"]),
        payload=expected_payload,
    )
    actual = harness_event_identity(
        session_id=event["session_id"],
        seq=event["seq"],
        event_type=event["type"],
        created_at=datetime.fromisoformat(event["ts"]),
        payload=event["payload"],
    )
    assert actual == expected
    return expected


def _assert_rust_persisted_d10(daemon, event, expected):
    """Rust must persist the exact canonical D10 key for the wire projection."""
    if os.environ.get("HARNESSD_IMPL") != "rs":
        return
    db_path = daemon.root / "home" / ".drover" / "harnessd" / "harnessd.sqlite"
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT dedup_key FROM events WHERE event_id = ?", (event["event_id"],)
        ).fetchone()
    assert row == (expected,)


@pytest.mark.parametrize(
    "daemon", [{"claude_fixture": "claude_permission.ndjson"}], indirect=True
)
def test_claude_stream_projection_permission_resume_interrupt_and_d10(daemon, fake_hub):
    # This fixture pauses at control_request and only emits assistant/result
    # after the daemon writes Claude's real control_response protocol.
    status, session = request(
        daemon,
        "/sessions",
        payload={
            "harness": "claude-code",
            "mode": "structured",
            "cwd": str(daemon.root),
        },
    )
    assert status == 201, session
    sid = session["session_id"]
    status, turn = request(daemon, f"/sessions/{sid}/turns", payload={"text": "go"})
    assert status == 202, turn
    wait_until(
        lambda: any(
            event["type"] == "approval_prompt" for event in _events(fake_hub, sid)
        )
    )
    status, answered = request(
        daemon,
        f"/sessions/{sid}/permission",
        payload={"request_id": "permit-1", "decision": "allow", "note": "fixture"},
    )
    assert (status, answered) == (200, {"ok": True})
    wait_until(
        lambda: request(daemon, f"/sessions/{sid}")[1].get("awaiting") == "input"
    )
    wait_until(
        lambda: {event["type"] for event in _events(fake_hub, sid)}
        >= {"approval_prompt", "approval_response", "assistant_output", "status"}
    )

    events = _events(fake_hub, sid)
    prompt = _event(events, "approval_prompt")
    _assert_d10_projection(
        prompt,
        {
            "request_id": "permit-1",
            "tool": "Bash",
            "input": {"command": "echo permitted"},
        },
    )
    response = _event(events, "approval_response")
    _assert_d10_projection(
        response,
        {"request_id": "permit-1", "decision": "allow", "note": "fixture"},
    )
    assistant = _event(events, "assistant_output", text="permission accepted")
    d10 = _assert_d10_projection(
        assistant,
        {
            "model": "fixture-model",
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "usage_message_id": "msg-fixture-1",
            "native_session_id": "claude-fixture-native",
            "native_event_id": "fixture-assistant",
            "text_stream_id": "msg-fixture-1:0",
            "text_stream_final": True,
        },
    )
    _assert_rust_persisted_d10(daemon, assistant, d10)

    # A process restart must use Claude's native ID and retain intervention.
    daemon.process.kill()
    daemon.process.wait(timeout=5)
    daemon.restart()
    assert request(daemon, f"/sessions/{sid}")[1]["status"] == "errored"
    status, recovered = request(
        daemon,
        f"/sessions/{sid}/recover",
        payload={"native_session_id": "claude-fixture-native"},
    )
    assert status == 200 and recovered["recovered"] is True
    wait_until(
        lambda: "--resume" in (daemon.root / "fake-cli.jsonl").read_text()
        and "claude-fixture-native" in (daemon.root / "fake-cli.jsonl").read_text()
    )
    assert request(daemon, f"/sessions/{sid}/interrupt", payload={}) == (
        200,
        {"ok": True},
    )


def test_deepseek_rpc_projection_interrupt_resume_and_d10(
    daemon, fake_hub, fake_deepseek
):
    status, intervention = request(
        daemon,
        "/sessions",
        payload={
            "harness": "deepseek-harness",
            "mode": "structured",
            "cwd": str(daemon.root),
        },
    )
    assert status == 201, intervention
    intervention_id = intervention["session_id"]
    assert (
        request(daemon, f"/sessions/{intervention_id}/turns", payload={"text": "wait"})[
            0
        ]
        == 202
    )
    # The active-turn handler invokes session.cancel against the real fake RPC
    # boundary, rather than merely returning a successful HTTP envelope.
    assert request(daemon, f"/sessions/{intervention_id}/interrupt", payload={}) == (
        200,
        {"ok": True},
    )
    assert any(item["method"] == "session.cancel" for item in fake_deepseek.requests)
    assert (
        request(daemon, f"/sessions/{intervention_id}/terminate", payload={})[0] == 200
    )

    status, session = request(
        daemon,
        "/sessions",
        payload={
            "harness": "deepseek-harness",
            "mode": "structured",
            "cwd": str(daemon.root),
        },
    )
    assert status == 201, session
    sid = session["session_id"]

    status, turn = request(daemon, f"/sessions/{sid}/turns", payload={"text": "hello"})
    assert status == 202, turn
    wait_until(
        lambda: request(daemon, f"/sessions/{sid}")[1].get("awaiting") == "input"
    )
    wait_until(
        lambda: {event["type"] for event in _events(fake_hub, sid)}
        >= {"assistant_output", "tool_action", "tool_result", "status"}
    )
    events = _events(fake_hub, sid)
    assistant = _event(events, "assistant_output", text="deepseek fixture")
    d10 = _assert_d10_projection(
        assistant,
        {
            "thinking": False,
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "native_session_id": "dsh-fixture-1",
        },
    )
    _assert_rust_persisted_d10(daemon, assistant, d10)
    tool = _event(events, "tool_action", text="Read")
    assert tool["payload"] == {
        "tool": "Read",
        "tool_use_id": "call-1",
        "input": {"path": "README.md"},
        "view": {"fixture": True},
        "native_session_id": "dsh-fixture-1",
    }

    daemon.process.kill()
    daemon.process.wait(timeout=5)
    daemon.restart()
    status, recovered = request(
        daemon,
        f"/sessions/{sid}/recover",
        payload={"native_session_id": "dsh-fixture-1"},
    )
    assert status == 200 and recovered["recovered"] is True
    assert request(daemon, f"/sessions/{sid}/terminate", payload={})[0] == 200
