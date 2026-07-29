"""Drive RelayManager over a socketpair; this test plays the spoke."""
import json
import socket
import threading

import pytest

from drover.server.harness.relay_protocol import (
    data_frame,
    open_error_frame,
    opened_frame,
    res_frame,
)
from drover.server.harness.websocket import client_recv_json, client_send_json
from drover.server.relay_manager import RelayManager, RelayUnavailable


def _attach(manager: RelayManager, host_id: str = "laptop"):
    hub_side, spoke_side = socket.socketpair()
    manager.attach(host_id, hub_side)
    return spoke_side


def _spoke_recv(spoke: socket.socket) -> dict:
    while True:
        frame = client_recv_json(spoke)
        if frame is not None:
            return frame


def test_request_round_trip() -> None:
    manager = RelayManager()
    spoke = _attach(manager)

    def spoke_loop() -> None:
        frame = _spoke_recv(spoke)
        assert frame["kind"] == "req"
        assert frame["method"] == "GET"
        assert frame["path"] == "/sessions"
        client_send_json(spoke, res_frame(frame["id"], 200, '{"sessions": []}\n'))

    thread = threading.Thread(target=spoke_loop, daemon=True)
    thread.start()
    status, body = manager.request("laptop", "GET", "/sessions", None, timeout_s=5)
    assert status == 200
    assert json.loads(body) == {"sessions": []}
    thread.join(timeout=5)


def test_request_to_unknown_host_is_502() -> None:
    manager = RelayManager()
    status, body = manager.request("ghost", "GET", "/sessions", None, timeout_s=1)
    assert status == 502
    assert "not connected" in body


def test_presence_flips_on_socket_death() -> None:
    manager = RelayManager()
    spoke = _attach(manager)
    assert manager.is_live("laptop")
    spoke.close()
    # request() must fail fast once the reader notices the dead socket
    status, _ = manager.request("laptop", "GET", "/sessions", None, timeout_s=5)
    assert status == 502
    assert not manager.is_live("laptop")
    assert "laptop" not in manager.live_host_ids()


def test_channel_open_data_close() -> None:
    manager = RelayManager()
    spoke = _attach(manager)

    def spoke_loop() -> None:
        frame = _spoke_recv(spoke)
        assert frame["kind"] == "open"
        chan = frame["chan"]
        client_send_json(spoke, opened_frame(chan))
        client_send_json(spoke, data_frame(chan, {"type": "output", "data": "hi"}))
        incoming = _spoke_recv(spoke)
        assert incoming == data_frame(chan, {"type": "stdin", "data": "ls\n"})

    thread = threading.Thread(target=spoke_loop, daemon=True)
    thread.start()
    channel = manager.open_channel("laptop", "/sessions/s1/terminal", timeout_s=5)
    assert channel.recv(timeout_s=5) == {"type": "output", "data": "hi"}
    channel.send({"type": "stdin", "data": "ls\n"})
    thread.join(timeout=5)
    channel.close()
    assert channel.closed


def test_channel_open_error_raises() -> None:
    manager = RelayManager()
    spoke = _attach(manager)

    def spoke_loop() -> None:
        frame = _spoke_recv(spoke)
        client_send_json(spoke, open_error_frame(frame["chan"], "unknown session"))

    threading.Thread(target=spoke_loop, daemon=True).start()
    with pytest.raises(RelayUnavailable):
        manager.open_channel("laptop", "/sessions/nope/terminal", timeout_s=5)
