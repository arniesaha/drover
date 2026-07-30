"""Small WebSocket framing helpers for drover-harnessd.

This intentionally implements only the subset harnessd needs: JSON text frames,
ping/pong, and close. It keeps the first Meta Harness data-plane slice free from
an ASGI server dependency.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
import socket
import struct
from typing import Any

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Bound on the handshake response header, which is read a byte at a time and
# so needs its own stop condition against a peer that never sends the blank
# line.
MAX_HANDSHAKE_HEAD_BYTES = 65536

# Largest frame we will read. Bounds three traffic classes that cross these
# sockets: JSON control frames, PTY chunks the daemon caps at 8192 bytes, and
# `res` frames carrying a proxied HTTP response body -- the largest of which
# is native-transcript, clipped per-record by the daemon but still able to
# reach ~1.2 MB across 100 records plus JSON overhead. 8 MiB keeps that
# comfortably inside the cap while still bounding the attack the cap exists
# for (a peer announcing a multi-gigabyte length to force an allocation).
MAX_FRAME_BYTES = 8 << 20

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


@dataclass(frozen=True)
class WebSocketFrame:
    opcode: int
    payload: bytes


class WebSocketClosed(Exception):
    """Raised when the peer closes or the TCP socket goes away."""


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1(f"{client_key}{WS_GUID}".encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def send_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    send_frame(sock, OPCODE_TEXT, json.dumps(payload, sort_keys=True).encode("utf-8"))


def recv_json(sock: socket.socket) -> dict[str, Any] | None:
    frame = recv_frame(sock)
    if frame.opcode == OPCODE_CLOSE:
        raise WebSocketClosed()
    if frame.opcode == OPCODE_PING:
        send_frame(sock, OPCODE_PONG, frame.payload)
        return None
    if frame.opcode == OPCODE_PONG:
        return None
    if frame.opcode != OPCODE_TEXT:
        return None
    loaded = json.loads(frame.payload.decode("utf-8"))
    return loaded if isinstance(loaded, dict) else None


def send_close(sock: socket.socket, *, code: int = 1000, reason: str = "") -> None:
    payload = struct.pack("!H", code) + reason.encode("utf-8")
    send_frame(sock, OPCODE_CLOSE, payload)


def send_frame(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    first = 0x80 | opcode
    length = len(payload)
    if length < 126:
        header = bytes([first, length])
    elif length < 65536:
        header = bytes([first, 126]) + struct.pack("!H", length)
    else:
        header = bytes([first, 127]) + struct.pack("!Q", length)
    sock.sendall(header + payload)


def recv_frame(sock: socket.socket) -> WebSocketFrame:
    header = _recv_exact(sock, 2)
    first, second = header
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > MAX_FRAME_BYTES:
        # A 64-bit length field taken on trust is an allocation primitive:
        # _recv_exact grows a bytearray until satisfied, so a peer announcing
        # a multi-gigabyte frame exhausts hub memory, and one that announces
        # it and then dribbles holds a thread and a growing buffer. Relevant
        # now that the hub is on a public funnel where the shared token is the
        # only gate. Close rather than skip: we cannot resynchronize a stream
        # without consuming the payload we just refused to read.
        raise WebSocketClosed(
            f"websocket frame of {length} bytes exceeds {MAX_FRAME_BYTES}"
        )
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return WebSocketFrame(opcode=opcode, payload=payload)


def client_handshake(
    sock: socket.socket,
    *,
    host: str,
    path: str,
    headers: dict[str, str] | None = None,
) -> str:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    extra = "".join(f"{name}: {value}\r\n" for name, value in (headers or {}).items())
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"{extra}"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    head = _recv_handshake_head(sock)
    if " 101 " not in head.split("\r\n", 1)[0]:
        raise RuntimeError(f"websocket handshake failed: {head.splitlines()[0]}")
    return key


def _recv_handshake_head(sock: socket.socket) -> str:
    """Read the response header block and NOT one byte more.

    Byte at a time on purpose. A bulk ``recv(4096)`` also swallows whatever
    the peer wrote immediately behind its 101, and there is nowhere to put
    those bytes: ``recv_frame`` reads straight from the socket, so anything
    this function buffers past the header is gone. harnessd greets every new
    terminal attach with a frame in its very next write, so on a fast link
    (loopback, or a segment that coalesces) the first thing the user was
    meant to see is silently dropped.

    Handshakes happen once per connection or channel, so the extra syscalls
    cost nothing next to a load-dependent hole at the head of every stream.
    """
    buffer = bytearray()
    while not buffer.endswith(b"\r\n\r\n"):
        if len(buffer) >= MAX_HANDSHAKE_HEAD_BYTES:
            raise WebSocketClosed("websocket handshake response header too large")
        chunk = sock.recv(1)
        if not chunk:
            raise WebSocketClosed("socket closed during handshake")
        buffer += chunk
    return buffer.decode("iso-8859-1")


def client_send_frame(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    """Send a masked frame (client role). RFC 6455 requires masking client->server."""
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes([0x80 | opcode, 0x80 | length])
    elif length < 65536:
        header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", length)
    else:
        header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", length)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(header + mask + masked)


def client_recv_json(sock: socket.socket) -> dict[str, Any] | None:
    """recv_json for the client role: pongs pings with a masked frame."""
    frame = recv_frame(sock)
    if frame.opcode == OPCODE_CLOSE:
        raise WebSocketClosed()
    if frame.opcode == OPCODE_PING:
        client_send_frame(sock, OPCODE_PONG, frame.payload)
        return None
    if frame.opcode == OPCODE_PONG:
        return None
    if frame.opcode != OPCODE_TEXT:
        return None
    loaded = json.loads(frame.payload.decode("utf-8"))
    return loaded if isinstance(loaded, dict) else None


def client_send_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    client_send_frame(
        sock, OPCODE_TEXT, json.dumps(payload, sort_keys=True).encode("utf-8")
    )


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise WebSocketClosed()
        chunks.extend(chunk)
    return bytes(chunks)
