"""Round-trip tests for client-role (masked) websocket helpers."""
import socket

import pytest

from drover.server.harness.websocket import (
    OPCODE_PING,
    OPCODE_PONG,
    OPCODE_TEXT,
    WebSocketClosed,
    client_recv_json,
    client_send_frame,
    recv_frame,
    send_close,
    send_frame,
    send_json,
)


def _pair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair()


def test_client_send_frame_is_masked_and_round_trips() -> None:
    client, server = _pair()
    try:
        client_send_frame(client, OPCODE_TEXT, b'{"a": 1}')
        raw = server.recv(2)
        assert raw[1] & 0x80  # mask bit set on client frames
    finally:
        client.close()
        server.close()


def test_client_send_frame_payload_decodes_via_recv_frame() -> None:
    client, server = _pair()
    try:
        client_send_frame(client, OPCODE_TEXT, b'{"a": 1}')
        frame = recv_frame(server)
        assert frame.opcode == OPCODE_TEXT
        assert frame.payload == b'{"a": 1}'
    finally:
        client.close()
        server.close()


def test_client_recv_json_reads_server_text_frame() -> None:
    client, server = _pair()
    try:
        send_json(server, {"kind": "req", "id": "1"})
        assert client_recv_json(client) == {"kind": "req", "id": "1"}
    finally:
        client.close()
        server.close()


def test_client_recv_json_pongs_ping_with_masked_frame() -> None:
    client, server = _pair()
    try:
        send_frame(server, OPCODE_PING, b"hb")
        assert client_recv_json(client) is None  # ping consumed
        pong = recv_frame(server)  # recv_frame unmasks masked frames
        assert pong.opcode == OPCODE_PONG
        assert pong.payload == b"hb"
    finally:
        client.close()
        server.close()


def test_client_recv_json_raises_on_close() -> None:
    client, server = _pair()
    try:
        send_close(server)
        with pytest.raises(WebSocketClosed):
            client_recv_json(client)
    finally:
        client.close()
        server.close()
