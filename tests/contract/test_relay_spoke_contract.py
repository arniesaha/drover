"""Hub-spoke relay behavior through a real launched harnessd."""

from __future__ import annotations

import json
import socket
import time

import pytest

from drover.server.harness.relay_protocol import open_frame, req_frame
from drover.server.harness.websocket import recv_json, send_json


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
