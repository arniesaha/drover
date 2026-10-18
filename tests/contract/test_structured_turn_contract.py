"""Structured turn sequencing and idempotency over public daemon seams."""

from __future__ import annotations

import base64

from .test_harnessd_contract import request
from .support import wait_until


def _create(daemon):
    status, created = request(
        daemon,
        "/sessions",
        payload={
            "harness": "claude-code",
            "mode": "structured",
            "cwd": str(daemon.root),
            "prompt": "first",
        },
    )
    assert status == 201
    wait_until(
        lambda: request(daemon, f"/sessions/{created['session_id']}")[1].get("awaiting")
        == "input"
    )
    return created["session_id"]


def test_structured_turn_appends_user_input_and_seq_is_monotonic(daemon, fake_hub):
    session_id = _create(daemon)
    assert (
        request(daemon, f"/sessions/{session_id}/turns", payload={"text": "second"})[0]
        == 202
    )
    wait_until(
        lambda: any(
            event.get("type") == "user_input" and event.get("text") == "second"
            for item in fake_hub.requests
            if item["path"] == "/harness/events"
            for event in item["body"]["events"]
        )
    )
    events = [
        event
        for item in fake_hub.requests
        if item["path"] == "/harness/events"
        for event in item["body"]["events"]
    ]
    assert [event["seq"] for event in events] == sorted(
        event["seq"] for event in events
    )
    user_inputs = [event for event in events if event.get("type") == "user_input"]
    assert [event.get("text") for event in user_inputs] == ["first", "second"]


def test_duplicate_turn_skips_attachment_and_preference_side_effects(daemon):
    session_id = _create(daemon)
    payload = {
        "text": "image",
        "client_turn_id": "1a8c547f-5d4c-4e21-a361-e688407b103c",
        "images": [
            {
                "media_type": "image/png",
                "data_base64": base64.b64encode(b"image").decode(),
            }
        ],
    }
    first_status, first = request(
        daemon, f"/sessions/{session_id}/turns", payload=payload
    )
    second_status, second = request(
        daemon, f"/sessions/{session_id}/turns", payload=payload
    )
    assert (first_status, second_status, first["turn_id"], second["turn_id"]) == (
        202,
        202,
        first["turn_id"],
        first["turn_id"],
    )
    attachment_dir = daemon.root / "home" / ".drover" / "attachments" / session_id
    assert len(list(attachment_dir.iterdir())) == 1
