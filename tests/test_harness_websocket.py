"""Tests for Meta Harness WebSocket terminal attach."""

from __future__ import annotations

import json
import socket
import threading
from time import monotonic
import urllib.request

from drover.schema import bootstrap
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    create_harness_server,
    register_daemon_host,
)
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.websocket import (
    WebSocketClosed,
    client_handshake,
    client_send_json,
    recv_frame,
)


class _FailingRegistry:
    def append_event(self, **kwargs):
        raise RuntimeError("locked")

    def append_transcript_chunk(self, **kwargs):
        raise RuntimeError("locked")

    def update_session_status(self, *args, **kwargs):
        raise RuntimeError("locked")


def _connect_ws(base_url: str, path: str) -> socket.socket:
    host_port = base_url.removeprefix("http://")
    host, port = host_port.split(":", 1)
    sock = socket.create_connection((host, int(port)), timeout=5)
    client_handshake(sock, host=host_port, path=path)
    sock.settimeout(5)
    return sock


def _json_request(url: str, *, payload: dict | None = None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _start_test_server(tmp_path):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    state = HarnessDaemonState(
        host_id="test-host",
        display_name="Test Host",
        kind="linux",
        registry=registry,
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        local_url="http://127.0.0.1:0",
    )
    register_daemon_host(state)
    server = create_harness_server(listen_host="127.0.0.1", listen_port=0, state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, state, f"http://{host}:{port}"


def _recv_json(sock: socket.socket) -> dict:
    frame = recv_frame(sock)
    if frame.opcode == 0x8:
        raise WebSocketClosed()
    return json.loads(frame.payload.decode("utf-8"))


def _wait_for_output(sock: socket.socket, needle: str) -> str:
    deadline = monotonic() + 5
    collected = ""
    while monotonic() < deadline:
        message = _recv_json(sock)
        if message.get("type") == "output":
            collected += message.get("data", "")
            if needle in collected:
                return collected
    raise AssertionError(f"did not observe {needle!r}; collected={collected!r}")


def _wait_for_normalized_event(sock: socket.socket, normalized_type: str) -> dict:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        message = _recv_json(sock)
        if message.get("type") != "event":
            continue
        event = message.get("event") or {}
        if event.get("normalized_type") == normalized_type:
            return event
    raise AssertionError(f"did not observe normalized event {normalized_type!r}")


def _wait_for_close(sock: socket.socket) -> None:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        frame = recv_frame(sock)
        if frame.opcode == 0x8:
            return
    raise AssertionError("websocket did not close")


def _wait_for_event(
    state: HarnessDaemonState, session_id: str, event_type: str
) -> None:
    deadline = monotonic() + 2
    while monotonic() < deadline:
        events = [event.event_type for event in state.registry.list_events(session_id)]
        if event_type in events:
            return
    raise AssertionError(f"event {event_type!r} was not recorded")


def test_terminal_websocket_sends_input_and_captures_transcript(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        _, created = _json_request(
            f"{base_url}/sessions",
            payload={"harness": "shell", "cwd": str(tmp_path)},
        )
        session_id = created["session_id"]
        sock = _connect_ws(base_url, f"/sessions/{session_id}/terminal")
        try:
            attached = _recv_json(sock)
            assert attached["type"] == "attached"

            client_send_json(sock, {"type": "resize", "rows": 30, "cols": 100})
            client_send_json(sock, {"type": "input", "data": "echo WS_OK\n"})

            input_event = _wait_for_normalized_event(sock, "command")
            assert input_event["normalized_source"] == "inferred_terminal"
            assert input_event["content_preview"] == "echo WS_OK"

            output = _wait_for_output(sock, "WS_OK")
            assert "WS_OK" in output

            client_send_json(sock, {"type": "ping"})
            pong = _recv_json(sock)
            while pong.get("type") in {"output", "event"}:
                pong = _recv_json(sock)
            assert pong["type"] == "pong"

            client_send_json(sock, {"type": "interrupt"})
            client_send_json(sock, {"type": "detach"})
            _wait_for_close(sock)
        finally:
            sock.close()

        chunks = state.registry.list_transcript_chunks(session_id)
        assert [chunk.sequence for chunk in chunks] == list(range(1, len(chunks) + 1))
        assert any("WS_OK" in chunk.content_redacted for chunk in chunks)

        _wait_for_event(state, session_id, "terminal.detached")
        events = [event.event_type for event in state.registry.list_events(session_id)]
        assert "terminal.attached" in events
        assert "terminal.input" in events
        normalized = {
            event.event_type: event.normalized_type
            for event in state.registry.list_events(session_id)
        }
        assert normalized["terminal.input"] == "command"
        assert normalized["terminal.output"] in {"assistant_output", "tool_action"}
        assert "terminal.resized" in events
        assert "terminal.interrupt" in events
        assert "terminal.detached" in events
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_terminal_websocket_rejects_unknown_session(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        host_port = base_url.removeprefix("http://")
        host, port = host_port.split(":", 1)
        sock = socket.create_connection((host, int(port)), timeout=5)
        try:
            try:
                client_handshake(sock, host=host_port, path="/sessions/nope/terminal")
            except RuntimeError as exc:
                assert "404" in str(exc)
            else:
                raise AssertionError("unknown terminal attach should fail")
        finally:
            sock.close()
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_client_handshake_sends_extra_headers():
    client_sock, server_sock = socket.socketpair()
    server_sock.settimeout(5)
    try:
        thread = threading.Thread(
            target=client_handshake,
            kwargs={
                "sock": client_sock,
                "host": "x",
                "path": "/",
                "headers": {"Authorization": "Bearer t"},
            },
            daemon=True,
        )
        thread.start()
        request_bytes = b""
        try:
            while b"\r\n\r\n" not in request_bytes:
                chunk = server_sock.recv(4096)
                if not chunk:
                    break
                request_bytes += chunk
        except OSError:
            pass
        server_sock.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"\r\n"
        )
        thread.join(timeout=5)
    finally:
        client_sock.close()
        server_sock.close()

    assert b"Authorization: Bearer t\r\n" in request_bytes


def test_terminal_websocket_streams_when_registry_writes_fail(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    session_id = "harness-locked-registry"
    state.registry = _FailingRegistry()
    state.pty.start(session_id=session_id, command="/bin/sh", cwd=tmp_path)
    try:
        sock = _connect_ws(base_url, f"/sessions/{session_id}/terminal")
        try:
            client_send_json(sock, {"type": "input", "data": "echo LOCK_OK\n"})
            assert "LOCK_OK" in _wait_for_output(sock, "LOCK_OK")
            client_send_json(sock, {"type": "detach"})
            _wait_for_close(sock)
        finally:
            sock.close()
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()
