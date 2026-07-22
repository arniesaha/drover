"""Tests for Meta Harness WebSocket terminal attach."""

from __future__ import annotations

import json
import socket
import threading
from time import monotonic
import urllib.request

import pytest

from drover.schema import bootstrap
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    HarnessPreset,
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


def _start_test_server(tmp_path, presets=None):
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
        presets=presets or DEFAULT_PRESETS,
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


def test_terminal_reattach_replays_scrollback(tmp_path):
    """A detach leaves the PTY running; a later attach must replay the
    buffered scrollback so the client is not staring at a blank terminal
    until the process happens to emit its next byte."""
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
            client_send_json(sock, {"type": "input", "data": "echo SCROLLBACK_OK\n"})
            _wait_for_output(sock, "SCROLLBACK_OK")
            client_send_json(sock, {"type": "detach"})
            _wait_for_close(sock)
        finally:
            sock.close()

        # Second attach: nothing new is typed, so the only way SCROLLBACK_OK
        # can arrive is the daemon replaying buffered output on attach.
        sock = _connect_ws(base_url, f"/sessions/{session_id}/terminal")
        try:
            attached = _recv_json(sock)
            assert attached["type"] == "attached"
            assert "SCROLLBACK_OK" in _wait_for_output(sock, "SCROLLBACK_OK")
        finally:
            sock.close()
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_handoff_seed_is_queued_then_typed_in_once_the_cli_settles(tmp_path):
    """The handoff seed must NOT be written at spawn (it races the CLI's cold
    start and is lost). It is queued and typed in by the terminal loop once the
    session's output settles, so it reliably lands in a ready shell."""
    server, state, base_url = _start_test_server(tmp_path)
    try:
        _, created = _json_request(
            f"{base_url}/sessions",
            payload={
                "harness": "shell",
                "cwd": str(tmp_path),
                # Executed output ("SEED_42") differs from the echoed command
                # text, so matching it proves the shell actually ran the seed
                # rather than merely line-echoing it.
                "initial_input": "echo SEED_$((6 * 7))\n",
            },
        )
        session_id = created["session_id"]
        # Queued at create, not written — nothing has driven the PTY yet.
        assert session_id in state.pending_initial_input

        sock = _connect_ws(base_url, f"/sessions/{session_id}/terminal")
        try:
            assert _recv_json(sock)["type"] == "attached"
            # Attaching drives the terminal loop, which types the seed in once
            # the shell settles; the shell then executes it.
            assert "SEED_42" in _wait_for_output(sock, "SEED_42")
            client_send_json(sock, {"type": "detach"})
            _wait_for_close(sock)
        finally:
            sock.close()

        # Consumed exactly once, and recorded as a handoff marker.
        assert session_id not in state.pending_initial_input
        _wait_for_event(state, session_id, "terminal.initial_input")
        normalized = {
            event.event_type: event.normalized_type
            for event in state.registry.list_events(session_id)
        }
        assert normalized["terminal.initial_input"] == "handoff_marker"
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "gate_prompt",
    [
        # Old claude-code trust-gate wording…
        "Do you trust the files in this folder?",
        # …and the reworded gate shipped around claude-code v2.1.x, which
        # resurrected the seed-swallow live on 2026-07-22 because only the
        # old wording was matched.
        "Is this a project you created or one you trust?",
    ],
)
def test_handoff_seed_waits_for_startup_gate_to_be_answered(tmp_path, gate_prompt):
    """A harness whose CLI opens on a startup gate (claude-code's trust-folder
    prompt) must have the gate answered before the seed is typed. Without gate
    handling, the settle-based delivery types the seed INTO the gate, which
    discards it — the handed-off agent starts with no context.

    The gate markers come from the real claude-code preset so this pins the
    production config against every known wording of the gate."""
    # Fake gated CLI: shows the trust prompt, and only reaches its "REPL"
    # (a real shell) if the gate is answered with exactly "1". Anything else
    # (e.g. the seed text) is reported as swallowed, mirroring claude-code.
    script = tmp_path / "gated_cli.sh"
    script.write_text(
        f'echo "{gate_prompt}"\n'
        "read answer\n"
        'if [ "$answer" = "1" ]; then\n'
        '  echo "REPL_READY"\n'
        "  exec /bin/sh\n"
        "else\n"
        '  echo "GATE_SWALLOWED:$answer"\n'
        "fi\n"
    )
    presets = dict(DEFAULT_PRESETS)
    presets["gated"] = HarnessPreset(
        name="gated",
        command=("/bin/sh", str(script)),
        enabled=True,
        description="fake CLI with a startup trust gate",
        startup_gate_markers=DEFAULT_PRESETS["claude-code"].startup_gate_markers,
        startup_gate_answer="1\n",
    )
    server, state, base_url = _start_test_server(tmp_path, presets=presets)
    try:
        _, created = _json_request(
            f"{base_url}/sessions",
            payload={
                "harness": "gated",
                "cwd": str(tmp_path),
                "initial_input": "echo SEED_$((6 * 7))\n",
            },
        )
        session_id = created["session_id"]
        sock = _connect_ws(base_url, f"/sessions/{session_id}/terminal")
        try:
            assert _recv_json(sock)["type"] == "attached"
            # The daemon must answer the gate ("1") first, then deliver the
            # seed into the shell behind it.
            collected = _wait_for_output(sock, "SEED_42")
            assert "REPL_READY" in collected
            assert "GATE_SWALLOWED" not in collected
            client_send_json(sock, {"type": "detach"})
            _wait_for_close(sock)
        finally:
            sock.close()
        assert session_id not in state.pending_initial_input
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_handoff_seed_dropped_when_session_ends_before_any_attach(tmp_path):
    """A queued seed whose session is terminated before a terminal ever
    attaches is discarded, not leaked."""
    server, state, base_url = _start_test_server(tmp_path)
    try:
        _, created = _json_request(
            f"{base_url}/sessions",
            payload={
                "harness": "shell",
                "cwd": str(tmp_path),
                "initial_input": "echo NEVER_DELIVERED\n",
            },
        )
        session_id = created["session_id"]
        assert session_id in state.pending_initial_input

        _json_request(f"{base_url}/sessions/{session_id}/terminate", payload={})
        assert session_id not in state.pending_initial_input
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()
