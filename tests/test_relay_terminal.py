"""Bridge a fake app websocket to a fake relay spoke through the hub."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from drover.schema import bootstrap
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.relay_protocol import (
    data_frame,
    hello_frame,
    opened_frame,
    res_frame,
)
from drover.server.harness.websocket import (
    OPCODE_PING,
    OPCODE_PONG,
    client_handshake,
    client_recv_json,
    client_send_frame,
    client_send_json,
    recv_frame,
    send_json,
)
from drover.server.metrics import MetricsCollector
from drover.server.web import app as app_module
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings


@dataclass
class _MetricsServerWithRelayHost:
    host: str
    port: int
    token: str
    collector: MetricsCollector
    server: object
    spoke_sock: socket.socket
    duckdb_path: object


def _spoke_recv(sock: socket.socket) -> dict:
    """Read frames, skipping pong/ping Nones, like test_relay_manager.py does."""
    while True:
        frame = client_recv_json(sock)
        if frame is not None:
            return frame


@pytest.fixture
def metrics_server_with_relay_host(tmp_path):
    """Server + connected fake spoke + session "s1" on relay host "laptop"."""
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="laptop",
        display_name="Laptop",
        kind="mac",
        connection_kind="relay",
        status="online",
    )
    registry.create_session(
        session_id="s1",
        host_id="laptop",
        harness="shell",
        command="/bin/sh",
        status="created",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
    )
    token = "test-token"
    collector.api_token = token
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(enabled=True, api_token=token),
    )
    host, port = server.server_address

    spoke = socket.create_connection((host, port), timeout=5)
    client_handshake(
        spoke,
        host=f"{host}:{port}",
        path="/harness/relay",
        headers={"Authorization": f"Bearer {token}"},
    )
    client_send_json(spoke, hello_frame("laptop"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if collector.relay_manager.is_live("laptop"):
            break
        time.sleep(0.05)
    assert collector.relay_manager.is_live("laptop")

    try:
        yield _MetricsServerWithRelayHost(
            host=host,
            port=port,
            token=token,
            collector=collector,
            server=server,
            spoke_sock=spoke,
            duckdb_path=duckdb_path,
        )
    finally:
        spoke.close()
        server.shutdown()
        server.server_close()


def test_terminal_attach_bridges_over_relay(metrics_server_with_relay_host):
    # env: server + connected fake spoke + session "s1" on host "laptop".
    env = metrics_server_with_relay_host
    spoke = env.spoke_sock
    errors: list[BaseException] = []

    def spoke_loop() -> None:
        try:
            # harness_terminal_route reconciles "created"/"starting"/"running"
            # sessions against the host before handing back a route -- for a
            # relay-connected host that reconcile GET rides the same socket,
            # so the fake spoke must answer it before the terminal "open"
            # frame.
            reconcile = _spoke_recv(spoke)
            assert reconcile["kind"] == "req"
            assert reconcile["method"] == "GET"
            assert reconcile["path"] == "/sessions/s1"
            client_send_json(spoke, res_frame(reconcile["id"], 200, "{}"))

            frame = _spoke_recv(spoke)
            assert frame["kind"] == "open"
            assert frame["path"] == "/sessions/s1/terminal"
            chan = frame["chan"]
            client_send_json(spoke, opened_frame(chan))
            client_send_json(spoke, data_frame(chan, {"type": "output", "data": "$ "}))
            # A PTY event frame, same shape the daemon's terminal loop sends
            # over a direct connection -- proves the relay flow mirrors it
            # into the hub's own event log via _mirror_harness_event_message.
            client_send_json(
                spoke,
                data_frame(
                    chan,
                    {
                        "type": "event",
                        "event": {
                            "event_id": "evt-relay-1",
                            "event_type": "terminal.output",
                            "payload": {"data": "$ "},
                        },
                    },
                ),
            )
            echo = _spoke_recv(spoke)
            assert echo["message"] == {"type": "stdin", "data": "ls\n"}
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            raise

    thread = threading.Thread(target=spoke_loop, daemon=True)
    thread.start()

    app = socket.create_connection((env.host, env.port), timeout=5)
    try:
        client_handshake(
            app,
            host=f"{env.host}:{env.port}",
            path="/harness/sessions/s1/terminal",
            headers={"Authorization": f"Bearer {env.token}"},
        )
        app.settimeout(5)
        first = None
        while first is None:
            first = client_recv_json(app)
        assert first == {"type": "output", "data": "$ "}
        second = None
        while second is None:
            second = client_recv_json(app)
        assert second == {
            "type": "event",
            "event": {
                "event_id": "evt-relay-1",
                "event_type": "terminal.output",
                "payload": {"data": "$ "},
            },
        }
        client_send_json(app, {"type": "stdin", "data": "ls\n"})
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors, f"spoke_loop raised: {errors}"
    finally:
        app.close()

    # The mirror is deliberately off the drain thread now, so it lands shortly
    # after the frame reaches the app rather than before it. Bounded poll.
    registry = HarnessRegistry(env.duckdb_path)
    deadline = time.monotonic() + 10
    mirrored = None
    while time.monotonic() < deadline:
        mirrored = registry.get_event("evt-relay-1")
        if mirrored is not None:
            break
        time.sleep(0.05)
    assert mirrored is not None
    assert mirrored.event_type == "terminal.output"
    assert mirrored.payload == {"data": "$ "}


def test_browser_pong_is_sent_under_the_write_lock(metrics_server_with_relay_host):
    """Pin the invariant: a pong must never interleave with a terminal frame.

    The app socket has two writers -- the forwarding thread and whichever
    thread answers a ping. Unsynchronized they desync the stream to the app
    permanently. The iOS client never pings, so today's trigger would be
    Tailscale Funnel, a new and unverified intermediary on this exact path.

    Rather than race it, this drives a ping through and asserts the pong went
    out holding the lock -- a refactor back to the module-level ``recv_json``
    (which pongs unlocked) leaves ``observed`` empty and fails here.
    """
    env = metrics_server_with_relay_host
    spoke = env.spoke_sock
    observed: list[tuple[int, bool]] = []
    sockets: list[app_module._BrowserSocket] = []
    real_init = app_module._BrowserSocket.__init__
    real_send_frame = app_module.send_frame

    def spy_init(self, sock):
        real_init(self, sock)
        sockets.append(self)

    def spy_send_frame(sock, opcode, payload=b""):
        locked = any(
            wrapper._write_lock.locked() for wrapper in sockets if wrapper.sock is sock
        )
        observed.append((opcode, locked))
        real_send_frame(sock, opcode, payload)

    def spoke_loop() -> None:
        reconcile = _spoke_recv(spoke)
        client_send_json(spoke, res_frame(reconcile["id"], 200, "{}"))
        frame = _spoke_recv(spoke)
        client_send_json(spoke, opened_frame(frame["chan"]))

    thread = threading.Thread(target=spoke_loop, daemon=True)
    thread.start()

    app_module._BrowserSocket.__init__ = spy_init
    app_module.send_frame = spy_send_frame
    try:
        app = socket.create_connection((env.host, env.port), timeout=5)
        try:
            client_handshake(
                app,
                host=f"{env.host}:{env.port}",
                path="/harness/sessions/s1/terminal",
                headers={"Authorization": f"Bearer {env.token}"},
            )
            thread.join(timeout=5)
            client_send_frame(app, OPCODE_PING, b"hb")
            app.settimeout(10)
            while recv_frame(app).opcode != OPCODE_PONG:
                pass
        finally:
            app.close()
    finally:
        app_module._BrowserSocket.__init__ = real_init
        app_module.send_frame = real_send_frame

    pongs = [locked for opcode, locked in observed if opcode == OPCODE_PONG]
    assert pongs, "the proxy answered the ping without going through send_frame"
    assert all(pongs), "pong was written to the app socket without the write lock"


def test_terminal_attach_never_dials_a_relay_host_by_url(
    metrics_server_with_relay_host,
):
    """Same rule as _harness_request, on the interactive path.

    A relay host whose row carries a URL must not be dialled when its socket
    is down: 127.0.0.1:7081 is the default listen address everywhere here, so
    the hub would attach the user's terminal to its own harnessd.
    """
    env = metrics_server_with_relay_host
    # A decoy standing in for "the hub's own harnessd on 127.0.0.1:7081".
    # Asserting on the app's status code would not discriminate -- a failed
    # upstream dial also yields 502 -- so the invariant under test is that
    # nothing ever arrives here.
    hits: list[str] = []

    class _Decoy(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits.append(self.path)
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    decoy = ThreadingHTTPServer(("127.0.0.1", 0), _Decoy)
    decoy_thread = threading.Thread(target=decoy.serve_forever, daemon=True)
    decoy_thread.start()

    HarnessRegistry(env.duckdb_path).register_host(
        host_id="laptop",
        display_name="Laptop",
        kind="mac",
        connection_kind="relay",
        local_url=f"http://127.0.0.1:{decoy.server_address[1]}",
    )
    env.spoke_sock.close()  # relay socket goes away; the stray URL remains
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not env.collector.relay_manager.is_live("laptop"):
            break
        time.sleep(0.05)
    assert not env.collector.relay_manager.is_live("laptop")

    app = socket.create_connection((env.host, env.port), timeout=5)
    try:
        with pytest.raises(RuntimeError) as caught:
            client_handshake(
                app,
                host=f"{env.host}:{env.port}",
                path="/harness/sessions/s1/terminal",
                headers={"Authorization": f"Bearer {env.token}"},
            )
        assert "502" in str(caught.value)
    finally:
        app.close()
        decoy.shutdown()
        decoy.server_close()
        decoy_thread.join(timeout=5)

    assert hits == [], f"hub dialled a relay host's URL for a terminal: {hits}"


def test_terminal_output_survives_a_wedged_event_mirror(
    metrics_server_with_relay_host, monkeypatch
):
    """The composition bug: a slow mirror must not eat terminal output.

    Before the mirror moved off the drain thread, every message paid a DuckDB
    connection under a process-wide lock. Under PTY burst rates the drain
    thread fell behind, the channel's bounded queue overflowed, and it dropped
    the *oldest* messages -- silently losing output the user was watching in
    order to finish a write nobody was waiting on.

    Here the writer is wedged outright, which is that failure taken to its
    limit: every frame must still reach the app.
    """
    env = metrics_server_with_relay_host
    spoke = env.spoke_sock
    blocked = threading.Event()

    def _wedged(self, records):
        blocked.wait(30)
        return 0

    monkeypatch.setattr(
        "drover.server.harness.registry.HarnessRegistry.append_events_if_new", _wedged
    )

    burst = 50
    errors: list[BaseException] = []

    def spoke_loop() -> None:
        try:
            reconcile = _spoke_recv(spoke)
            client_send_json(spoke, res_frame(reconcile["id"], 200, "{}"))
            frame = _spoke_recv(spoke)
            chan = frame["chan"]
            client_send_json(spoke, opened_frame(chan))
            for index in range(burst):
                # Every message is an event, so every one hits the mirror.
                client_send_json(
                    spoke,
                    data_frame(
                        chan,
                        {
                            "type": "event",
                            "event": {
                                "event_id": f"evt-burst-{index}",
                                "event_type": "terminal.output",
                                "payload": {"n": index},
                            },
                        },
                    ),
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=spoke_loop, daemon=True)
    thread.start()

    app = socket.create_connection((env.host, env.port), timeout=5)
    try:
        client_handshake(
            app,
            host=f"{env.host}:{env.port}",
            path="/harness/sessions/s1/terminal",
            headers={"Authorization": f"Bearer {env.token}"},
        )
        app.settimeout(15)
        seen = []
        while len(seen) < burst:
            message = client_recv_json(app)
            if message is not None:
                seen.append(message["event"]["payload"]["n"])
        assert seen == list(range(burst)), "terminal output was dropped or reordered"
        assert not errors, f"spoke_loop raised: {errors}"
    finally:
        blocked.set()
        app.close()
        thread.join(timeout=5)


def test_event_mirror_close_does_not_leak_a_parked_worker_thread():
    """N3: close()'s sentinel put is best-effort and can be dropped.

    If the queue is exactly full when close() runs, `put_nowait(None)` is
    silently suppressed. Before the `_closed` flag, the worker would then
    drain its backlog and block forever in a plain `queue.get()`, leaking a
    thread (and the registry it closes over) for the life of the process --
    precisely under the overload the class exists to survive.
    """
    started = threading.Event()
    wedge = threading.Event()

    class _WedgedRegistry:
        def append_events_if_new(self, records):
            started.set()
            wedge.wait(10)
            return len(records)

    mirror = app_module._EventMirror(_WedgedRegistry())
    try:
        # Get the worker parked inside append_events_if_new so everything
        # offered after this piles up behind it instead of draining live.
        mirror.offer({"event_id": "e-first"})
        assert started.wait(5), "worker never started its first batch"

        # Fill the queue to capacity so close()'s put_nowait(None) is
        # guaranteed to hit queue.Full and be dropped.
        for index in range(app_module.MIRROR_QUEUE_MAX):
            mirror.offer({"event_id": f"e-{index}"})

        mirror.close()
    finally:
        wedge.set()

    mirror._thread.join(timeout=5)
    assert not mirror._thread.is_alive(), "worker thread parked forever after close()"


def test_event_mirror_retries_a_transient_registry_failure():
    attempts = 0
    persisted = []

    class _FlakyRegistry:
        def append_events_if_new(self, records):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("TransactionException: write-write conflict")
            persisted.extend(records)
            return len(records)

    mirror = app_module._EventMirror(_FlakyRegistry())
    mirror.offer({"event_id": "e-retry", "session_id": "s1"})
    mirror.close()
    mirror._thread.join(timeout=5)

    assert not mirror._thread.is_alive()
    assert attempts == 2
    assert [record["event_id"] for record in persisted] == ["e-retry"]


def test_event_mirror_retains_a_batch_across_retry_cycles_while_attached():
    attempts = 0
    persisted = threading.Event()

    class _RecoveringRegistry:
        def append_events_if_new(self, records):
            nonlocal attempts
            attempts += 1
            if attempts <= app_module.MIRROR_WRITE_ATTEMPTS:
                raise RuntimeError("registry temporarily unavailable")
            persisted.set()
            return len(records)

    mirror = app_module._EventMirror(_RecoveringRegistry())
    try:
        mirror.offer({"event_id": "e-retained", "session_id": "s1"})
        assert persisted.wait(5), "failed batch was not offered on a later cycle"
    finally:
        mirror.close()
        mirror._thread.join(timeout=5)

    assert attempts == app_module.MIRROR_WRITE_ATTEMPTS + 1


def test_event_mirror_counts_and_marks_a_permanent_write_failure():
    from drover.server.harness import daemon as daemon_module

    daemon_module.reset_dropped_event_count()
    attempts = 0
    gaps = []

    class _FailingRegistry:
        def append_events_if_new(self, records):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("registry unavailable")

        def append_event(self, **kwargs):
            gaps.append(kwargs)

    mirror = app_module._EventMirror(_FailingRegistry())
    mirror.offer({"event_id": "e-lost", "session_id": "s1"})
    mirror.close()
    mirror._thread.join(timeout=5)

    assert attempts == 3
    assert daemon_module.dropped_event_count() == 1
    assert gaps == [
        {
            "session_id": "s1",
            "event_type": "transcript.gap",
            "payload": {"dropped": 1},
            "normalized_type": "status",
        }
    ]


class _StubChannel:
    """A relay channel with a fixed backlog, then nothing (recv -> None)."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.closed = False
        self.recv_calls = 0

    def recv(self, timeout_s: float):
        del timeout_s
        self.recv_calls += 1
        if not self._messages:
            return None
        return self._messages.pop(0)

    def close(self) -> None:
        self.closed = True


class _RecordingMirror:
    def __init__(self):
        self.offered = []

    def offer(self, record) -> None:
        self.offered.append(record)


def _event_message(event_id: str, event_type: str):
    return {
        "type": "event",
        "event": {
            "event_id": event_id,
            "event_type": event_type,
            "payload": {"text": "hello-relay"},
        },
    }


def test_closing_terminal_channel_rescues_its_undrained_events():
    """Issue #90: a detach used to discard events already on the channel.

    harnessd sends a PTY read's raw ``output`` frame before the
    ``terminal.output`` event frame that records it, so the event frame is
    the most likely thing in flight when the app side goes away. The
    forwarding loop returns the moment ``stop`` is set, and closing the
    channel then dropped whatever it had not read -- silently, with nothing
    counting it. This is the CI flake in ``test_relay_e2e`` ("hub never
    mirrored a terminal.output event"): under load the event never arrived
    at all, not late.
    """
    channel = _StubChannel(
        [
            {"type": "output", "data": "hello-relay\n"},
            _event_message("e-out", "terminal.output"),
        ]
    )
    mirror = _RecordingMirror()

    rescued = app_module._drain_channel_into_mirror(channel, "s1", mirror)

    assert rescued == 1
    assert [record["event_type"] for record in mirror.offered] == ["terminal.output"]
    assert mirror.offered[0]["event_id"] == "e-out"


def test_channel_drain_stops_on_an_empty_channel_without_spinning():
    """The common path: nothing buffered, so teardown pays one poll."""
    channel = _StubChannel([])
    mirror = _RecordingMirror()

    assert app_module._drain_channel_into_mirror(channel, "s1", mirror) == 0
    assert channel.recv_calls == 1
    assert mirror.offered == []


def test_channel_drain_is_bounded_against_a_peer_that_keeps_talking():
    """Teardown runs on a request-handler thread; a chatty peer must not own
    it. The count bound has to hold even when recv never returns None."""

    class _EndlessChannel:
        def __init__(self):
            self.n = 0

        def recv(self, timeout_s: float):
            del timeout_s
            self.n += 1
            return _event_message(f"e-{self.n}", "terminal.output")

    mirror = _RecordingMirror()
    rescued = app_module._drain_channel_into_mirror(_EndlessChannel(), "s1", mirror)

    assert rescued == app_module.MIRROR_DRAIN_MAX


def test_channel_drain_never_raises_out_of_teardown():
    """A channel that errors on recv must not break the finally block."""

    class _AngryChannel:
        def recv(self, timeout_s: float):
            del timeout_s
            raise OSError("channel is gone")

    mirror = _RecordingMirror()
    assert app_module._drain_channel_into_mirror(_AngryChannel(), "s1", mirror) == 0


def test_closing_direct_socket_rescues_its_undrained_events():
    upstream, hub = socket.socketpair()
    mirror = _RecordingMirror()
    try:
        send_json(upstream, {"type": "output", "data": "hello-direct\n"})
        send_json(upstream, _event_message("e-direct", "terminal.output"))
        upstream.shutdown(socket.SHUT_WR)

        rescued = app_module._drain_socket_into_mirror(hub, "s1", mirror)

        assert rescued == 1
        assert [record["event_id"] for record in mirror.offered] == ["e-direct"]
    finally:
        upstream.close()
        hub.close()
