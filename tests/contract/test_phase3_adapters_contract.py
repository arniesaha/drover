"""Claude Code and DeepSeek structured contracts shared by both daemons."""

from __future__ import annotations

from .support import wait_until
from .test_harnessd_contract import request


def test_deepseek_rpc_turn_events_worktree_and_interrupt(daemon, fake_hub):
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
    assert session["cwd"] == str(daemon.root)
    assert request(daemon, f"/sessions/{sid}/interrupt", payload={}) == (200, {"ok": True})

    status, turn = request(daemon, f"/sessions/{sid}/turns", payload={"text": "hello"})
    assert status == 202, turn
    wait_until(lambda: request(daemon, f"/sessions/{sid}")[1].get("awaiting") == "input")
    wait_until(
        lambda: {event["type"] for item in fake_hub.requests if item["path"] == "/harness/events" for event in item["body"]["events"]}
        >= {"assistant_output", "tool_action", "tool_result", "status"}
    )
    events = [
        event
        for item in fake_hub.requests
        if item["path"] == "/harness/events"
        for event in item["body"]["events"]
    ]
    kinds = {event["type"] for event in events}
    assert {"assistant_output", "tool_action", "tool_result", "status"} <= kinds
    assert any(
        event["payload"].get("native_session_id") == "dsh-fixture-1"
        for event in events
    )
    assert request(daemon, f"/sessions/{sid}/terminate", payload={})[0] == 200


def test_claude_capability_and_permission_surface(daemon):
    _, capabilities = request(daemon, "/capabilities")
    claude = next(item for item in capabilities["harnesses"] if item["name"] == "claude-code")
    assert claude["capabilities"]["approvals"] is True
    assert claude["capabilities"]["native_resume"] is True
