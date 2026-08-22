"""Tests for Meta Harness WebSocket terminal attach."""

from __future__ import annotations

import json
import socket
import threading
import urllib.request
from time import monotonic
from time import sleep as time_sleep

import pytest

from drover.schema import bootstrap
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    HarnessPreset,
    create_harness_server,
    register_daemon_host,
)
from drover.server.harness.models import HarnessEvent
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.websocket import (
    WebSocketClosed,
    client_handshake,
    client_send_json,
    recv_frame,
)
from drover.server.metrics import MetricsCollector
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import DISABLED


class _FailingRegistry:
    def append_event(self, **kwargs):
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


def test_wire_payload_rejects_null_canonical_event_metadata():
    event = HarnessEvent(
        event_id="legacy-e1",
        session_id="legacy",
        event_type="assistant_output",
        payload={"text": "hello"},
        seq=None,
    )

    with pytest.raises(ValueError, match="sequence"):
        event.wire_payload()


def test_wire_payload_drops_the_duplicated_command_output():
    """Shell output is carried by `text`; the payload copy is dead weight.

    The codex adapter no longer writes it, but every event already on disk
    still has it — measured live at 992KB across 236 tool results, for a
    field with exactly one reference in the codebase: the line that used to
    produce it. Stripping here rather than only at the source is what makes
    existing sessions smaller immediately, instead of waiting for their
    history to age out of the window.
    """
    event = HarnessEvent(
        event_id="e1",
        session_id="s1",
        event_type="tool_result",
        payload={
            "text": "lots of shell output\n",
            "payload": {
                "tool": "shell",
                "exit_code": 0,
                "aggregated_output": "lots of shell output\n",
            },
        },
        seq=4,
    )

    wire = event.wire_payload()

    assert "aggregated_output" not in wire["payload"]
    assert wire["text"] == "lots of shell output\n"
    assert wire["payload"]["exit_code"] == 0
    assert wire["payload"]["tool"] == "shell"


def test_wire_payload_leaves_other_payloads_untouched():
    event = HarnessEvent(
        event_id="e2",
        session_id="s1",
        event_type="assistant_output",
        payload={"text": "hello", "payload": {"thinking": True}},
        seq=5,
    )

    wire = event.wire_payload()

    assert wire["payload"] == {"thinking": True}


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

        outputs = [
            event
            for event in state.registry.list_events(session_id)
            if event.event_type == "terminal.output"
        ]
        assert outputs, "terminal output must be recorded as events"
        assert any(
            "WS_OK" in (event.payload or {}).get("text", "") for event in outputs
        )

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


class _SlowWritesRegistry:
    """Delegates to a real registry but holds every write for ``slow_s`` —
    a stand-in for the process-wide DuckDB connect lock under contention
    (fleet renders, event ingest from every host). Terminal echo latency
    must not scale with this delay; only durable recording may."""

    def __init__(self, inner, slow_s: float = 0.2):
        self._inner = inner
        self._slow_s = slow_s

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in {"append_event", "append_events_if_new"}:

            def slowed(*args, **kwargs):
                time_sleep(self._slow_s)
                return attr(*args, **kwargs)

            return slowed
        return attr


def test_terminal_echo_is_not_serialized_behind_registry_writes(tmp_path):
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
            _wait_for_output(sock, "$")

            # From here on, every registry write stalls 200ms. A burst of five
            # keystrokes must still echo promptly: with per-keystroke writes
            # in the echo path (input event + transcript chunk + output
            # event) the fifth echo takes ~3s; decoupled, it takes ~PTY time.
            real_registry = state.registry
            state.registry = _SlowWritesRegistry(real_registry)

            start = monotonic()
            for ch in "abcde":
                client_send_json(sock, {"type": "input", "data": ch})
            _wait_for_output(sock, "abcde")
            elapsed = monotonic() - start
            assert elapsed < 1.5, (
                f"echo of 5-key burst took {elapsed:.2f}s — echo delivery is "
                "serialized behind registry writes"
            )

            # Durability is still required, just not on the echo's clock:
            # the mirrored events and transcript must land eventually.
            deadline = monotonic() + 10
            while monotonic() < deadline:
                # Breathe between polls: each list_* call takes the same
                # process-wide connect lock the mirror's writer needs.
                time_sleep(0.2)
                events = real_registry.list_events(session_id)
                types = [e.event_type for e in events]
                transcript = "".join(
                    (e.payload or {}).get("text", "")
                    for e in events
                    if e.event_type == "terminal.output"
                )
                if (
                    types.count("terminal.input") >= 5
                    and "terminal.output" in types
                    and "abcde" in transcript
                ):
                    break
            else:
                raise AssertionError(
                    "mirrored input/output events or transcript never recorded"
                )
        finally:
            sock.close()
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_direct_hub_echo_is_not_serialized_behind_registry_writes(
    tmp_path, monkeypatch
):
    """A hub audit write must never sit between a key and its echoed output.

    The host daemon emits a ``terminal.input`` event immediately after writing
    the PTY, then emits the PTY's echoed ``output`` on its next loop.  A direct
    hub used to persist that input event inline, so every keystroke waited for
    DuckDB before the echo could even be read from the upstream socket.
    """
    harness_server, harness_state, harness_url = _start_test_server(
        tmp_path / "harness"
    )
    hub_server = None
    release_writes = threading.Event()
    try:
        _, created = _json_request(
            f"{harness_url}/sessions",
            payload={"harness": "shell", "cwd": str(tmp_path)},
        )
        session_id = created["session_id"]

        hub_db = tmp_path / "hub" / "drover.duckdb"
        bootstrap(parquet_dir=tmp_path / "hub" / "parquet", duckdb_path=hub_db)
        hub_registry = HarnessRegistry(hub_db)
        hub_registry.register_host(
            host_id="direct-host",
            display_name="Direct Host",
            kind="linux",
            local_url=harness_url,
            connection_kind="direct",
            capabilities={"harnesses": [{"name": "shell", "enabled": True}]},
        )
        hub_registry.create_session(
            session_id=session_id,
            host_id="direct-host",
            harness="shell",
            command="/bin/sh",
            status="running",
            cwd=str(tmp_path),
        )
        collector = MetricsCollector(
            duckdb_path=hub_db,
            incoming_dir=tmp_path / "hub" / "incoming",
            summarizer_report={},
        )
        hub_server = start_metrics_server(
            host="127.0.0.1", port=0, collector=collector, auth=DISABLED
        )
        hub_host, hub_port = hub_server.server_address
        sock = socket.create_connection((hub_host, hub_port), timeout=5)
        try:
            client_handshake(
                sock,
                host=f"{hub_host}:{hub_port}",
                path=f"/harness/sessions/{session_id}/terminal",
            )
            sock.settimeout(5)
            assert _recv_json(sock)["type"] == "attached"
            _wait_for_output(sock, "$")

            original_append = HarnessRegistry.append_events_if_new
            writer_entered = threading.Event()

            def wedged_append(self, records):
                writer_entered.set()
                release_writes.wait(10)
                return original_append(self, records)

            monkeypatch.setattr(HarnessRegistry, "append_events_if_new", wedged_append)

            client_send_json(sock, {"type": "input", "data": "x"})
            assert writer_entered.wait(2), "the input event never reached a mirror"
            sock.settimeout(0.75)
            output = _wait_for_output(sock, "x")
            assert "x" in output

            # The speedup is decoupling, not dropping: once the registry is
            # writable again, the queued input event still reaches the hub.
            release_writes.set()
            deadline = monotonic() + 5
            while monotonic() < deadline:
                if any(
                    event.event_type == "terminal.input"
                    for event in hub_registry.list_events(session_id)
                ):
                    break
                time_sleep(0.05)
            else:
                raise AssertionError("the hub never persisted the queued input event")
        finally:
            release_writes.set()
            sock.close()
    finally:
        release_writes.set()
        harness_state.pty.close_all()
        harness_server.shutdown()
        harness_server.server_close()
        if hub_server is not None:
            hub_server.shutdown()
            hub_server.server_close()


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
        # …the reworded gate shipped around claude-code v2.1.x, which
        # resurrected the seed-swallow live on 2026-07-22 because only the
        # old wording was matched…
        "Is this a project you created or one you trust?",
        # …and that same wording as claude-code's ink renderer actually emits
        # it: words separated by cursor-positioning escapes instead of spaces,
        # so the sentence never appears contiguously in the raw PTY stream.
        "Is\x1b[1C\x1b[39mthis\x1b[1Ca\x1b[1Cproject\x1b[1Cyou\x1b[1C"
        "created\x1b[1Cor\x1b[1Cone\x1b[1Cyou\x1b[1Ctrust?",
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


def _serve_101_then_frame(server: socket.socket, payload: bytes) -> None:
    """Answer a handshake and write the 101 + a frame in a single send.

    This is what harnessd does on every terminal attach: it upgrades and
    greets with "attached" immediately, so on loopback the two land together.
    """
    from drover.server.harness.websocket import accept_key

    head = b""
    while b"\r\n\r\n" not in head:
        chunk = server.recv(4096)
        if not chunk:
            return
        head += chunk
    key = ""
    for line in head.decode("iso-8859-1").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n"
    ).encode("ascii")
    server.sendall(response + bytes([0x81, len(payload)]) + payload)


def test_client_handshake_does_not_swallow_the_first_frame():
    """A bulk header read eats whatever shares the 101's segment.

    recv_frame reads straight from the socket, so anything the handshake
    buffers past the header block is unrecoverable -- and the frame most
    likely to share that segment is the daemon's "attached" greeting, i.e.
    the first thing the user sees.
    """
    server, client = socket.socketpair()
    payload = json.dumps({"type": "attached"}, sort_keys=True).encode("utf-8")
    thread = threading.Thread(
        target=_serve_101_then_frame, args=(server, payload), daemon=True
    )
    thread.start()
    try:
        client_handshake(client, host="127.0.0.1:1", path="/sessions/s1/terminal")
        thread.join(timeout=5)
        client.settimeout(5)
        frame = recv_frame(client)
        assert json.loads(frame.payload.decode("utf-8")) == {"type": "attached"}
    finally:
        client.close()
        server.close()


def test_client_handshake_rejects_an_endless_header():
    """Byte-at-a-time reading needs its own bound against a peer that dribbles."""
    from drover.server.harness import websocket as websocket_module

    server, client = socket.socketpair()

    def flood() -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = server.recv(4096)
            if not chunk:
                return
            head += chunk
        try:
            # A 101 line, then headers forever and never the blank line.
            server.sendall(b"HTTP/1.1 101 Switching Protocols\r\n")
            while True:
                server.sendall(b"X-Pad: " + b"a" * 512 + b"\r\n")
        except OSError:
            return

    thread = threading.Thread(target=flood, daemon=True)
    thread.start()
    try:
        client.settimeout(30)
        with pytest.raises(WebSocketClosed):
            client_handshake(client, host="127.0.0.1:1", path="/relay")
    finally:
        client.close()
        server.close()
        thread.join(timeout=5)

    assert websocket_module.MAX_HANDSHAKE_HEAD_BYTES <= 1 << 20


def test_recv_frame_refuses_an_oversized_announced_length():
    """A 64-bit length taken on trust is an allocation primitive.

    _recv_exact grows a bytearray until satisfied, so a peer announcing a
    multi-gigabyte frame exhausts hub memory without ever sending the bytes.
    Post-auth, but the shared token is the only gate on a public funnel.
    """
    from drover.server.harness.websocket import MAX_FRAME_BYTES

    server, client = socket.socketpair()
    try:
        # Header only: 8-byte extended length announcing 8 GiB, no payload.
        server.sendall(bytes([0x81, 127]) + (8 * 1024**3).to_bytes(8, "big"))
        client.settimeout(5)
        with pytest.raises(WebSocketClosed) as caught:
            recv_frame(client)
        assert "exceeds" in str(caught.value)
    finally:
        client.close()
        server.close()

    assert MAX_FRAME_BYTES >= 65536, "must still clear real PTY and JSON frames"


def test_recv_frame_still_accepts_a_large_but_legal_frame():
    """The cap must not clip anything these sockets legitimately carry."""
    from drover.server.harness.websocket import MAX_FRAME_BYTES, send_frame

    server, client = socket.socketpair()
    payload = b"x" * 65536  # 8x the daemon's PTY read size
    assert len(payload) <= MAX_FRAME_BYTES
    thread = threading.Thread(
        target=lambda: send_frame(server, 0x1, payload), daemon=True
    )
    thread.start()
    try:
        client.settimeout(5)
        assert recv_frame(client).payload == payload
    finally:
        thread.join(timeout=5)
        client.close()
        server.close()


def test_recv_frame_round_trips_a_two_megabyte_frame():
    """`res` frames proxy whole HTTP bodies -- native-transcript can approach
    ~1.2 MB (see N2 in the relay re-review). The cap must clear that with
    comfortable margin, not just PTY-sized chunks.
    """
    from drover.server.harness.websocket import MAX_FRAME_BYTES, send_frame

    server, client = socket.socketpair()
    payload = b"y" * (2 * 1024 * 1024)
    assert len(payload) <= MAX_FRAME_BYTES
    thread = threading.Thread(
        target=lambda: send_frame(server, 0x1, payload), daemon=True
    )
    thread.start()
    try:
        client.settimeout(10)
        assert recv_frame(client).payload == payload
    finally:
        thread.join(timeout=10)
        client.close()
        server.close()


def test_recv_frame_refuses_a_frame_over_the_eight_mebibyte_cap():
    """The cap itself must land at 8 MiB, not silently drift back down."""
    from drover.server.harness.websocket import MAX_FRAME_BYTES

    assert MAX_FRAME_BYTES == 8 * 1024 * 1024

    server, client = socket.socketpair()
    try:
        length = MAX_FRAME_BYTES + 1
        server.sendall(bytes([0x81, 127]) + length.to_bytes(8, "big"))
        client.settimeout(5)
        with pytest.raises(WebSocketClosed) as caught:
            recv_frame(client)
        assert "exceeds" in str(caught.value)
    finally:
        client.close()
        server.close()


def test_recv_frame_scoped_cap_rejects_before_payload_read():
    """A caller-specific cap must be checked from the frame header alone."""
    server, client = socket.socketpair()
    try:
        length = 4097
        # Header only: reading the mask or payload after seeing the scoped cap
        # blocks until timeout and fails the test for the wrong exception.
        server.sendall(bytes([0x81, 0x80 | 127]) + length.to_bytes(8, "big"))
        client.settimeout(0.5)
        with pytest.raises(WebSocketClosed) as caught:
            recv_frame(client, max_frame_bytes=4096)
        assert "4096" in str(caught.value)
    finally:
        client.close()
        server.close()


def test_mirror_retries_then_counts_dropped_events():
    """A write failure must be retried, and a permanent one must be counted.

    The old bare `except Exception: pass` lost events silently and forever.
    """
    from drover.server.harness import daemon as daemon_mod

    daemon_mod.reset_dropped_event_count()
    attempts = {"n": 0}

    class AlwaysFailingRegistry:
        def append_events_if_new(self, records):
            attempts["n"] += 1
            raise RuntimeError("TransactionException: write-write conflict")

        def append_event(self, **kwargs):
            return None

    mirror = daemon_mod._TerminalMirror(AlwaysFailingRegistry())
    mirror.record_event({"event_id": "e1", "session_id": "s1"})
    mirror.stop()

    assert attempts["n"] == 3, "should retry twice after the first failure"
    assert daemon_mod.dropped_event_count() == 1


def test_mirror_retry_succeeds_without_counting_a_drop():
    from drover.server.harness import daemon as daemon_mod

    daemon_mod.reset_dropped_event_count()
    attempts = {"n": 0}

    class FlakyRegistry:
        def append_events_if_new(self, records):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("TransactionException")
            return len(records)

        def append_event(self, **kwargs):
            raise AssertionError("no gap marker expected on eventual success")

    mirror = daemon_mod._TerminalMirror(FlakyRegistry())
    mirror.record_event({"event_id": "e1", "session_id": "s1"})
    mirror.stop()

    assert attempts["n"] == 2
    assert daemon_mod.dropped_event_count() == 0


def test_terminal_mirror_bounds_backlog_and_records_overflow_gap():
    """A blocked registry must cost history, never unbounded process memory."""

    from drover.server.harness import daemon as daemon_mod

    daemon_mod.reset_dropped_event_count()
    writer_entered = threading.Event()
    release_writer = threading.Event()
    gaps = []

    class BlockedRegistry:
        def append_events_if_new(self, records):
            writer_entered.set()
            assert release_writer.wait(3)
            return len(records)

        def append_event(self, **kwargs):
            gaps.append(kwargs)

    mirror = daemon_mod._TerminalMirror(BlockedRegistry())
    mirror.record_event({"event_id": "first", "session_id": "s1"})
    assert writer_entered.wait(1)

    overflow = 7
    for index in range(daemon_mod.TERMINAL_MIRROR_QUEUE_MAX + overflow):
        mirror.record_event({"event_id": f"e-{index}", "session_id": "s1"})

    assert mirror._queue.qsize() == daemon_mod.TERMINAL_MIRROR_QUEUE_MAX
    assert daemon_mod.dropped_event_count() == overflow

    release_writer.set()
    mirror.stop()

    assert sum(item["payload"]["dropped"] for item in gaps) == overflow
