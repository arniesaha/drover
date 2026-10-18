"""Public terminal authorization and structured attachment contracts."""

from __future__ import annotations

import base64

import pytest

from .test_harnessd_contract import request


def _structured(daemon, **extra):
    return request(
        daemon,
        "/sessions",
        payload={
            "harness": "claude-code",
            "mode": "structured",
            "cwd": str(daemon.root),
            **extra,
        },
    )


def _image():
    return {
        "media_type": "image/png",
        "data_base64": base64.b64encode(b"image").decode(),
    }


def test_daemon_terminal_attach_rejects_unauthenticated(daemon):
    status, body = request(daemon, "/sessions/nope/terminal", token=False)
    assert (status, body) == (401, {"error": "authentication required"})


def test_initial_prompt_with_image_saves_file_and_records_attachment(daemon):
    status, created = _structured(daemon, prompt="describe", images=[_image()])
    assert status == 201
    attachment_dir = (
        daemon.root / "home" / ".drover" / "attachments" / created["session_id"]
    )
    assert [path.suffix for path in attachment_dir.iterdir()] == [".png"]


def test_turn_with_image_saves_file_and_appends_path(daemon):
    status, created = _structured(daemon)
    assert status == 201
    status, turn = request(
        daemon,
        f"/sessions/{created['session_id']}/turns",
        payload={"text": "see", "images": [_image()]},
    )
    assert status == 202
    assert isinstance(turn["turn_id"], str)
    attachment_dir = (
        daemon.root / "home" / ".drover" / "attachments" / created["session_id"]
    )
    assert [path.suffix for path in attachment_dir.iterdir()] == [".png"]


def test_image_only_turn_is_accepted(daemon):
    status, created = _structured(daemon)
    assert status == 201
    status, turn = request(
        daemon,
        f"/sessions/{created['session_id']}/turns",
        payload={"images": [_image()]},
    )
    assert status == 202
    assert isinstance(turn["turn_id"], str)


@pytest.mark.parametrize("mode", ["auto", None])
def test_structured_permission_mode_is_publicly_accepted(daemon, mode):
    payload = {} if mode is None else {"permission_mode": mode}
    status, created = _structured(daemon, **payload)
    assert status == 201
    assert (
        request(daemon, f"/sessions/{created['session_id']}")[1]["mode"] == "structured"
    )
