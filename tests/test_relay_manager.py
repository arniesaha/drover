"""Drive RelayManager over a socketpair; this test plays the spoke."""

import json
import socket
import threading

import pytest

from drover.server import relay_manager
from drover.server.harness.relay_protocol import (
    data_frame,
    open_error_frame,
    opened_frame,
    res_frame,
)
from drover.server.harness.websocket import (
    OPCODE_PING,
    OPCODE_PONG,
    client_recv_json,
    client_send_frame,
    client_send_json,
    recv_frame,
)
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


# --- regression tests ------------------------------------------------------
# These reach into manager internals on purpose: the invariants they pin
# (write-lock discipline, bounded writes) are not observable from the public
# surface, and a refactor that breaks them must fail loudly here.


def test_attach_replaces_previous_connection() -> None:
    """Newest wins: the displaced socket is closed and the new one serves."""
    manager = RelayManager()
    first_hub, first_spoke = socket.socketpair()
    manager.attach("laptop", first_hub)
    second_hub, second_spoke = socket.socketpair()
    manager.attach("laptop", second_hub)

    # attach() tears the old connection down synchronously, so the old spoke
    # sees EOF immediately - no polling, no sleeping.
    first_spoke.settimeout(5)
    assert first_spoke.recv(1) == b""
    assert manager.is_live("laptop")
    assert manager.live_host_ids() == {"laptop"}

    def spoke_loop() -> None:
        frame = _spoke_recv(second_spoke)
        client_send_json(second_spoke, res_frame(frame["id"], 200, "second"))

    thread = threading.Thread(target=spoke_loop, daemon=True)
    thread.start()
    assert manager.request("laptop", "GET", "/sessions", None, timeout_s=5) == (
        200,
        "second",
    )
    thread.join(timeout=5)


def test_in_flight_request_fails_when_socket_dies() -> None:
    """A request already on the wire must not wait out its own timeout."""
    manager = RelayManager()
    spoke = _attach(manager)
    result: dict[str, tuple[int, str]] = {}

    def caller() -> None:
        # Generous timeout: only the teardown path can make this return fast.
        result["value"] = manager.request(
            "laptop", "GET", "/sessions", None, timeout_s=30
        )

    thread = threading.Thread(target=caller, daemon=True)
    thread.start()
    assert _spoke_recv(spoke)["kind"] == "req"  # request is in flight
    spoke.close()
    thread.join(timeout=10)
    assert not thread.is_alive()
    status, body = result["value"]
    assert status == 502
    assert "relay connection lost" in body
    assert not manager.is_live("laptop")


def test_pong_is_sent_under_the_write_lock() -> None:
    """Pin the invariant: a pong must never interleave with a hub frame.

    Answering a ping outside the write lock lets pong bytes land mid-frame and
    desync the stream. A refactor back to ``recv_json`` (which pongs
    internally, unlocked) makes ``observed`` empty and fails here.
    """
    manager = RelayManager()
    spoke = _attach(manager)
    connection = manager._connections["laptop"]
    observed: list[tuple[int, bool]] = []
    real_send_frame = relay_manager.send_frame

    def spy(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
        observed.append((opcode, connection.write_lock.locked()))
        real_send_frame(sock, opcode, payload)

    relay_manager.send_frame = spy
    try:
        client_send_frame(spoke, OPCODE_PING, b"hb")
        spoke.settimeout(5)
        while recv_frame(spoke).opcode != OPCODE_PONG:
            pass
    finally:
        relay_manager.send_frame = real_send_frame

    pongs = [locked for opcode, locked in observed if opcode == OPCODE_PONG]
    assert pongs, "reader answered the ping without going through send_frame"
    assert all(pongs), "pong was sent without holding the write lock"


def test_request_gives_up_when_write_path_is_wedged() -> None:
    """A wedged writer must not swallow the caller's timeout."""
    manager = RelayManager()
    spoke = _attach(manager)  # keep a reference: a GC'd spoke closes the socket
    assert manager.is_live("laptop")
    connection = manager._connections["laptop"]
    result: dict[str, tuple[int, str]] = {}

    def caller() -> None:
        result["value"] = manager.request(
            "laptop", "GET", "/sessions", None, timeout_s=1.0
        )

    # Run the caller on its own thread so an unbounded acquire fails this test
    # instead of hanging it.
    connection.write_lock.acquire()  # stand in for a sendall that never returns
    thread = threading.Thread(target=caller, daemon=True)
    try:
        thread.start()
        thread.join(timeout=10)
        stuck = thread.is_alive()
    finally:
        connection.write_lock.release()

    assert not stuck, "request ignored timeout_s while the write path was wedged"
    status, body = result["value"]
    assert status == 502
    assert "wedged" in body
    # A connection whose write path is wedged is dropped, not left a zombie.
    assert not manager.is_live("laptop")
    spoke.close()


def test_reader_still_dispatches_while_write_path_is_wedged() -> None:
    """Head-of-line blocking: a busy write path must not stall dispatch."""
    manager = RelayManager()
    spoke = _attach(manager)
    connection = manager._connections["laptop"]
    result: dict[str, tuple[int, str]] = {}

    def caller() -> None:
        result["value"] = manager.request(
            "laptop", "GET", "/sessions", None, timeout_s=10
        )

    thread = threading.Thread(target=caller, daemon=True)
    thread.start()
    frame = _spoke_recv(spoke)
    connection.write_lock.acquire()  # wedge the write path after the send
    try:
        # The ping would block a reader that waits indefinitely for the write
        # lock, leaving the response below undispatched.
        client_send_frame(spoke, OPCODE_PING, b"hb")
        client_send_json(spoke, res_frame(frame["id"], 200, "answered"))
        thread.join(timeout=8)
        assert not thread.is_alive()
    finally:
        connection.write_lock.release()
    assert result["value"] == (200, "answered")


def test_channel_queue_drops_oldest_instead_of_growing() -> None:
    """Backpressure: an unread channel is bounded, newest messages survive."""
    manager = RelayManager()
    spoke = _attach(manager)

    def spoke_loop() -> None:
        frame = _spoke_recv(spoke)
        client_send_json(spoke, opened_frame(frame["chan"]))

    thread = threading.Thread(target=spoke_loop, daemon=True)
    thread.start()
    channel = manager.open_channel("laptop", "/sessions/s1/terminal", timeout_s=5)
    thread.join(timeout=5)

    overflow = relay_manager.CHANNEL_QUEUE_MAX + 50
    for index in range(overflow):
        channel._offer({"n": index})
    assert channel._incoming.qsize() <= relay_manager.CHANNEL_QUEUE_MAX
    assert channel._dropped == 50
    # Oldest dropped, newest kept: the first surviving message is not n=0.
    assert channel.recv(timeout_s=5) == {"n": 50}
