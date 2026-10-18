"""Hub-spoke relay behavior through a real launched harnessd."""

from __future__ import annotations

import json
import socket
import time

import pytest

from drover.server.harness.relay_protocol import data_frame, open_frame, req_frame
from drover.server.harness.websocket import recv_json, send_json

from .test_harnessd_contract import request


@pytest.mark.parametrize("daemon", [True], indirect=True)
def test_relay_hello_and_loopback_sessions_request(daemon, fake_hub):
    sock = fake_hub.relay()
    hello = next(item for item in fake_hub.requests if item["path"] == "/harness/relay")
    assert hello["authorization"] == "Bearer contract-token"
    assert hello["body"]["kind"] == "hello"
    assert hello["body"]["host_id"] == "contract-host"
    send_json(sock, req_frame("sessions", "GET", "/sessions", None))
    response = recv_json(sock)
    assert response["kind"] == "res"
    assert response["id"] == "sessions"
    assert response["status"] == 200
    assert json.loads(response["body"])["sessions"] == []


@pytest.mark.parametrize("daemon", [True], indirect=True)
def test_relay_open_unknown_terminal_returns_open_error(daemon, fake_hub):
    sock = fake_hub.relay()
    send_json(sock, open_frame("missing", "/sessions/nope/terminal"))
    deadline = time.monotonic() + 5
    response = None
    while time.monotonic() < deadline:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        try:
            frame = recv_json(sock)
        except socket.timeout:
            break
        if (
            frame
            and frame.get("kind") == "open_error"
            and frame.get("chan") == "missing"
        ):
            response = frame
            break
    assert response is not None, "relay did not return open_error for missing terminal"
    assert response["kind"] == "open_error"
    assert response["chan"] == "missing"


@pytest.mark.parametrize("daemon", [True], indirect=True)
def test_relay_terminal_channel_streams_shell_output_and_input(daemon, fake_hub):
    status, session = request(
        daemon,
        "/sessions",
        payload={"harness": "shell", "cwd": str(daemon.root), "command": ["/bin/sh"]},
    )
    assert status == 201
    sock = fake_hub.relay()
    send_json(sock, open_frame("shell", f"/sessions/{session['session_id']}/terminal"))
    assert recv_json(sock) == {"kind": "opened", "chan": "shell"}
    attached = recv_json(sock)
    assert attached["kind"] == "data"
    assert attached["message"] == {
        "type": "attached",
        "session_id": session["session_id"],
    }
    typed = "printf 'relay-%s\\n' ok\n"
    send_json(sock, data_frame("shell", {"type": "input", "data": typed}))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        message = recv_json(sock)
        data = message.get("message", {}).get("data", "") if message else ""
        if (
            message
            and message.get("kind") == "data"
            and "relay-ok" in data
            and data != typed
        ):
            return
    raise AssertionError("terminal output did not traverse the relay")
