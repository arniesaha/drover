"""Spoke-side relay client: one outbound websocket from harnessd to the hub.

A harnessd behind NAT cannot be dialled, so it dials out instead and holds a
single websocket open. Everything the hub would normally do over inbound HTTP
rides that one socket: ``req``/``res`` frames become loopback HTTP calls
against this daemon's own listener, and ``open``/``data``/``close`` frames
become loopback terminal websockets.

Concurrency shape (why the write lock exists): the frame loop thread, one
worker per in-flight ``req``, one worker per channel open, and one pump per
live channel all write to the same hub socket. Client-role frames are masked,
so two writers interleaving mid-frame desyncs the stream permanently - every
outbound byte, including pong replies, goes through ``_Conn.send*`` and its
lock. That is also why the frame loop reads with ``recv_frame`` rather than
``client_recv_json``: the latter pongs unlocked.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import random
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

from drover.server.harness.relay_protocol import (
    FRAMED_RESPONSES_CAPABILITY,
    RelayProtocolError,
    close_frame,
    data_frame,
    hello_frame,
    open_error_frame,
    opened_frame,
    parse_frame,
    res_frame,
    res_start_frame,
)
from drover.server.harness.websocket import (
    OPCODE_CLOSE,
    OPCODE_PING,
    OPCODE_PONG,
    OPCODE_TEXT,
    WebSocketClosed,
    client_handshake,
    client_send_frame,
    client_send_json,
    recv_frame,
)

log = logging.getLogger("drover.relay.client")

RELAY_PATH = "/harness/relay"
CONNECT_TIMEOUT_S = 15.0
# Longer than the hub's 20s ping interval, so a healthy idle connection is
# never mistaken for a dead one. Without it a NAT that silently drops the flow
# would leave this daemon blocked in recv forever - unreachable, and never
# reconnecting. We do not ping the hub ourselves: its pings are the liveness
# signal, and a missing pong is explicitly not proof of death (the hub drops
# pongs when its own write path is busy).
READ_TIMEOUT_S = 90.0
# Matched to the hub's default request budget rather than exceeding it by 2x.
# Work the hub has already given up on is work nobody will ever read, and on
# a polled endpoint every second past the hub's deadline is another orphaned
# loopback call holding a DuckDB-contending connection open on this machine.
LOOPBACK_TIMEOUT_S = 15.0
# Ceiling on concurrent ``req`` workers. Each one is a loopback HTTP call, so
# unbounded spawn under a hub burst - or under a polled endpoint whose budget
# keeps expiring - turns into unbounded threads and unbounded database
# contention on a laptop. Refusing fast is strictly better than accepting work
# that will miss its deadline anyway.
MAX_INFLIGHT_REQS = 16
WRITE_TIMEOUT_S = 10.0
# The frame loop skips a pong rather than stall on a busy write path: a
# dropped pong is harmless, a stalled reader sits on frames already buffered.
PONG_WRITE_TIMEOUT_S = 0.5
CLOSE_WRITE_TIMEOUT_S = 1.0
MAX_BACKOFF_S = 300.0
# A connection that lasted this long counts as healthy: the next failure
# starts backing off from zero again.
STABLE_CONNECTION_S = 60.0
# How long stop() waits for the client thread. Bounded because a dial already
# in flight cannot be interrupted; the thread is a daemon, so a wedged dial
# delays nothing at process exit.
STOP_JOIN_TIMEOUT_S = 5.0

# ws/wss are accepted as aliases so a websocket-looking URL does the right
# thing rather than silently downgrading to plaintext port 80.
_SCHEME_ALIASES = {"http": "http", "https": "https", "ws": "http", "wss": "https"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


class _WriteTimeout(RuntimeError):
    """The connection's write path did not free up in time (internal)."""


class RelayConfigError(ValueError):
    """The relay target is unusable no matter how many times we redial."""


def _read_bounded_loopback_body(
    response: Any, *, max_response_bytes: int | None
) -> str:
    if max_response_bytes is None:
        return response.read().decode("utf-8", errors="replace")
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")
    content_length = response.getheader("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError as exc:
            raise ValueError("loopback response has invalid Content-Length") from exc
        if declared_bytes < 0:
            raise ValueError("loopback response has invalid Content-Length")
        if declared_bytes > max_response_bytes:
            raise ValueError("loopback response exceeds byte limit")
    payload = response.read(max_response_bytes + 1)
    if len(payload) > max_response_bytes:
        raise ValueError("loopback response exceeds byte limit")
    return payload.decode("utf-8", errors="replace")


class _Target:
    """A validated dial target parsed out of ``central_url``."""

    def __init__(self, central_url: str) -> None:
        parsed = urlparse(central_url)
        raw_scheme = (parsed.scheme or "").lower()
        scheme = _SCHEME_ALIASES.get(raw_scheme)
        if scheme is None:
            # Guessing here is how a Bearer token ends up on the wire in the
            # clear: an unknown scheme used to mean "no TLS, port 80".
            raise RelayConfigError(
                f"unsupported relay URL scheme {raw_scheme or '(none)'!r} in "
                f"{central_url!r}; expected one of "
                f"{', '.join(sorted(_SCHEME_ALIASES))}"
            )
        host = parsed.hostname
        if not host:
            raise RelayConfigError(f"relay URL has no host: {central_url!r}")
        self.scheme = scheme
        self.host = host
        self.port = parsed.port or _DEFAULT_PORTS[scheme]
        self.netloc = parsed.netloc or f"{host}:{self.port}"
        self.path = f"{(parsed.path or '').rstrip('/')}{RELAY_PATH}"
        self.tls = scheme == "https"


class _Channel:
    """One terminal attach stream: a loopback websocket to our own daemon."""

    def __init__(self, chan: str) -> None:
        self.chan = chan
        self.sock: socket.socket | None = None
        # The frame loop writes ``data`` here while the pump may answer a
        # ping; both are client-role masked frames on the same socket.
        self.write_lock = threading.Lock()
        self.closed = threading.Event()
        self._close_lock = threading.Lock()

    def mark_closed(self) -> bool:
        """Flip to closed exactly once."""
        with self._close_lock:
            if self.closed.is_set():
                return False
            self.closed.set()
            return True

    def send_local(self, message: dict[str, Any]) -> None:
        sock = self.sock
        if sock is None:
            raise WebSocketClosed(f"relay channel not open: {self.chan}")
        with self.write_lock:
            client_send_json(sock, message)

    def pong_local(self, payload: bytes) -> None:
        sock = self.sock
        if sock is None:
            return
        with self.write_lock:
            client_send_frame(sock, OPCODE_PONG, payload)

    def shutdown(self) -> None:
        sock = self.sock
        if sock is None:
            return
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            sock.close()


class _Conn:
    """One live hub websocket plus every channel multiplexed over it.

    Per-connection rather than per-client so that workers left over from a
    dropped connection can never write onto its replacement's socket.
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.write_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.channels: dict[str, _Channel] = {}
        self.alive = threading.Event()
        self.alive.set()

    @contextlib.contextmanager
    def write_access(self, timeout_s: float) -> Iterator[None]:
        if not self.write_lock.acquire(timeout=max(timeout_s, 0.0)):
            raise _WriteTimeout("relay write path wedged")
        try:
            yield
        finally:
            self.write_lock.release()

    def send(self, payload: dict[str, Any], timeout_s: float = WRITE_TIMEOUT_S) -> None:
        with self.write_access(timeout_s):
            client_send_json(self.sock, payload)

    def send_control(
        self, opcode: int, payload: bytes = b"", timeout_s: float = WRITE_TIMEOUT_S
    ) -> None:
        with self.write_access(timeout_s):
            client_send_frame(self.sock, opcode, payload)

    def send_bounded_response(self, request_id: str, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        with self.write_access(WRITE_TIMEOUT_S):
            client_send_json(
                self.sock, res_start_frame(request_id, status, len(payload))
            )
            client_send_frame(self.sock, OPCODE_TEXT, payload)

    def add_channel(self, channel: _Channel) -> bool:
        with self.state_lock:
            if not self.alive.is_set():
                return False
            self.channels[channel.chan] = channel
            return True

    def get_channel(self, chan: str) -> _Channel | None:
        with self.state_lock:
            return self.channels.get(chan)

    def forget_channel(self, chan: str) -> _Channel | None:
        with self.state_lock:
            return self.channels.pop(chan, None)

    def drain_channels(self) -> list[_Channel]:
        with self.state_lock:
            channels = list(self.channels.values())
            self.channels.clear()
            return channels


class RelayClient:
    """Dials the hub and services relay frames until told to stop."""

    def __init__(
        self, central_url: str, host_id: str, token: str, loopback_port: int
    ) -> None:
        self.central_url = central_url
        self.host_id = host_id
        self.token = token
        self.loopback_port = loopback_port
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: _Conn | None = None
        self._logged_failure = False
        # Per client, not per connection: it bounds this machine's total
        # loopback load, which a reconnect does not reset.
        self._req_slots = threading.BoundedSemaphore(MAX_INFLIGHT_REQS)
        self._refused_reqs = 0

    # -- lifecycle -----------------------------------------------------

    def start(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.run_forever, name="drover-relay-client", daemon=True
        )
        self._thread = thread
        thread.start()
        return thread

    def stop(self) -> None:
        """Stop reconnecting and drop the live connection, if any.

        A dial already in flight cannot be interrupted, so ``stop`` may return
        while ``_connect`` is still blocked in ``create_connection``. That is
        why ``serve_connection`` re-checks ``_stopped``: a connection that
        completes *after* this call must be torn down, not served.
        """
        self._stopped.set()
        connection = self._conn
        if connection is not None:
            self._teardown(connection, "relay client stopping")
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=STOP_JOIN_TIMEOUT_S)
            if thread.is_alive():
                log.debug("relay client thread still winding down after stop()")

    def run_forever(self) -> None:
        try:
            target = _Target(self.central_url)
        except RelayConfigError as exc:
            # Redialling cannot fix a typo. Mirrors run_harnessd's handling of
            # --relay without a token: log loudly, keep serving locally.
            log.error("relay disabled: %s", exc)
            return
        attempt = 0
        while not self._stopped.is_set():
            started = time.monotonic()
            try:
                sock = self._connect(target)
            except Exception as exc:  # noqa: BLE001 - any dial failure retries
                self._log_failure(f"relay connect to {self.central_url} failed", exc)
            else:
                self._logged_failure = False
                log.info("relay connected to %s as %s", self.central_url, self.host_id)
                self.serve_connection(sock)
            if self._stopped.is_set():
                return
            if time.monotonic() - started >= STABLE_CONNECTION_S:
                attempt = 0
            delay = min(MAX_BACKOFF_S, float(2**attempt)) * random.uniform(0.5, 1.5)
            attempt = min(attempt + 1, 16)
            self._stopped.wait(delay)

    def _log_failure(self, message: str, exc: BaseException) -> None:
        """First failure is worth seeing; a laptop offline for a weekend is not."""
        if self._logged_failure:
            log.debug("%s: %s", message, exc)
            return
        self._logged_failure = True
        log.warning("%s: %s", message, exc)

    def _connect(self, target: _Target) -> socket.socket:
        raw = socket.create_connection(
            (target.host, target.port), timeout=CONNECT_TIMEOUT_S
        )
        sock = raw
        if target.tls:
            try:
                sock = ssl.create_default_context().wrap_socket(
                    raw, server_hostname=target.host
                )
            except Exception:
                raw.close()
                raise
        try:
            client_handshake(
                sock,
                host=target.netloc,
                path=target.path,
                headers={"Authorization": f"Bearer {self.token}"},
            )
            client_send_json(
                sock,
                hello_frame(
                    self.host_id,
                    capabilities=[FRAMED_RESPONSES_CAPABILITY],
                ),
            )
        except Exception:
            with contextlib.suppress(OSError):
                sock.close()
            raise
        return sock

    # -- frame loop ----------------------------------------------------

    def serve_connection(self, sock: socket.socket) -> None:
        """Run the frame loop on an already-connected socket until it dies.

        Never raises: this is a thread target, and every death path here is
        just a reconnect for ``run_forever``.
        """
        with contextlib.suppress(OSError):
            sock.settimeout(READ_TIMEOUT_S)
        connection = _Conn(sock)
        self._conn = connection
        if self._stopped.is_set():
            # stop() ran while this connection was still being dialled, so it
            # never saw it in _conn. Serving it now would outlive the daemon.
            self._teardown(connection, "relay client stopped during connect")
            self._conn = None
            return
        try:
            self._read_loop(connection)
        except (
            WebSocketClosed,
            OSError,
            RelayProtocolError,
            json.JSONDecodeError,
        ) as exc:
            log.info("relay connection to %s ended: %s", self.central_url, exc)
        except Exception:  # noqa: BLE001 - a loop crash must still tear down
            log.exception("relay frame loop crashed")
        finally:
            self._teardown(connection, "relay frame loop stopped")
            if self._conn is connection:
                self._conn = None

    def _read_loop(self, connection: _Conn) -> None:
        while connection.alive.is_set():
            frame = recv_frame(connection.sock)
            if frame.opcode == OPCODE_CLOSE:
                raise WebSocketClosed()
            if frame.opcode == OPCODE_PING:
                self._pong(connection, frame.payload)
                continue
            if frame.opcode != OPCODE_TEXT:
                continue  # pongs and anything else we do not speak
            payload = json.loads(frame.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                continue
            self._dispatch(connection, parse_frame(payload))

    def _pong(self, connection: _Conn, payload: bytes) -> None:
        try:
            connection.send_control(
                OPCODE_PONG, payload, timeout_s=PONG_WRITE_TIMEOUT_S
            )
        except _WriteTimeout:
            log.warning("relay pong dropped: write path busy")

    def _dispatch(self, connection: _Conn, frame: dict[str, Any]) -> None:
        kind = frame["kind"]
        if kind == "req":
            # Own thread: a slow session create must not stall terminal data.
            self._start_req(connection, frame)
        elif kind == "open":
            self._handle_open(connection, frame)
        elif kind == "data":
            self._handle_data(connection, frame)
        elif kind == "close":
            channel = connection.forget_channel(str(frame.get("chan")))
            if channel is not None:
                # Hub initiated: closing it back would be an echo.
                self._close_channel(connection, channel, notify_hub=False)
        else:  # pragma: no cover - the hub never originates these
            log.warning("unexpected relay frame kind from hub: %s", kind)

    def _spawn(self, target: Any, *args: Any, name: str) -> None:
        threading.Thread(target=target, args=args, name=name, daemon=True).start()

    def _send(self, connection: _Conn, payload: dict[str, Any]) -> None:
        """Best-effort outbound frame; a dead socket just ends the connection."""
        if not connection.alive.is_set():
            return
        try:
            connection.send(payload)
        except _WriteTimeout as exc:
            # Lock-acquisition timeout: our frame never started, so the stream
            # is still in sync. We drop the connection anyway - 10s of not
            # getting the lock means another writer is stuck in sendall, which
            # means the peer stopped draining, which means it is gone.
            self._teardown(connection, str(exc))
        except (OSError, WebSocketClosed) as exc:
            self._teardown(connection, f"relay send failed: {exc}")

    def _send_bounded_response(
        self, connection: _Conn, request_id: str, status: int, body: str
    ) -> None:
        if not connection.alive.is_set():
            return
        try:
            connection.send_bounded_response(request_id, status, body)
        except _WriteTimeout as exc:
            self._teardown(connection, str(exc))
        except (OSError, WebSocketClosed) as exc:
            self._teardown(connection, f"relay send failed: {exc}")

    def _send_response(
        self,
        connection: _Conn,
        request: dict[str, Any],
        status: int,
        body: str,
    ) -> None:
        request_id = str(request.get("id"))
        if request.get("response_framing") == FRAMED_RESPONSES_CAPABILITY:
            self._send_bounded_response(connection, request_id, status, body)
        else:
            self._send(connection, res_frame(request_id, status, body))

    # -- req/res -------------------------------------------------------

    def _start_req(self, connection: _Conn, frame: dict[str, Any]) -> None:
        """Take a worker slot, or answer 503 without taking a thread."""
        if not self._req_slots.acquire(blocking=False):
            self._refused_reqs += 1
            if self._refused_reqs == 1 or self._refused_reqs % 100 == 0:
                log.warning(
                    "relay refused %d request(s): %d already in flight",
                    self._refused_reqs,
                    MAX_INFLIGHT_REQS,
                )
            self._send_response(
                connection,
                frame,
                503,
                json.dumps(
                    {"error": "harness host is saturated"},
                    sort_keys=True,
                ),
            )
            return
        try:
            self._spawn(self._handle_req, connection, frame, name="relay-req")
        except Exception:  # noqa: BLE001 - a thread that never started holds nothing
            self._req_slots.release()
            raise

    def _handle_req(self, connection: _Conn, frame: dict[str, Any]) -> None:
        method = str(frame.get("method") or "GET").upper()
        path = str(frame.get("path") or "/")
        try:
            try:
                max_response_bytes = frame.get("max_response_bytes")
                if max_response_bytes is None:
                    status, body = self._loopback_request(
                        method, path, frame.get("body")
                    )
                elif type(max_response_bytes) is int and max_response_bytes > 0:
                    status, body = self._loopback_request(
                        method,
                        path,
                        frame.get("body"),
                        max_response_bytes=max_response_bytes,
                    )
                else:
                    raise ValueError("invalid relay response byte limit")
            except Exception as exc:  # noqa: BLE001 - never crash the frame loop
                log.warning("relay loopback %s %s failed: %s", method, path, exc)
                status = 502
                body = json.dumps(
                    {"error": f"loopback request failed: {exc}"}, sort_keys=True
                )
            self._send_response(connection, frame, status, body)
        finally:
            self._req_slots.release()

    def _loopback_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        max_response_bytes: int | None = None,
    ) -> tuple[int, str]:
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.loopback_port, timeout=LOOPBACK_TIMEOUT_S
        )
        try:
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            conn.request(method, path, body=json.dumps(body or {}), headers=headers)
            response = conn.getresponse()
            return response.status, _read_bounded_loopback_body(
                response, max_response_bytes=max_response_bytes
            )
        finally:
            conn.close()

    # -- channels ------------------------------------------------------

    def _handle_open(self, connection: _Conn, frame: dict[str, Any]) -> None:
        chan = str(frame.get("chan"))
        path = str(frame.get("path") or "/")
        channel = _Channel(chan)
        # Register before dialling: a ``close`` that lands while the loopback
        # handshake is still in flight must be able to cancel it, or the pump
        # would outlive the hub's interest in the channel.
        if not connection.add_channel(channel):
            return
        self._spawn(self._open_channel, connection, channel, path, name="relay-open")

    def _open_channel(self, connection: _Conn, channel: _Channel, path: str) -> None:
        try:
            sock = socket.create_connection(
                ("127.0.0.1", self.loopback_port), timeout=CONNECT_TIMEOUT_S
            )
        except OSError as exc:
            connection.forget_channel(channel.chan)
            channel.mark_closed()
            self._send(connection, open_error_frame(channel.chan, str(exc)))
            return
        try:
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
            client_handshake(
                sock, host=f"127.0.0.1:{self.loopback_port}", path=path, headers=headers
            )
        except Exception as exc:  # noqa: BLE001 - 404/426/reset all mean "no"
            with contextlib.suppress(OSError):
                sock.close()
            connection.forget_channel(channel.chan)
            channel.mark_closed()
            self._send(connection, open_error_frame(channel.chan, str(exc)))
            return
        sock.settimeout(None)
        channel.sock = sock
        if channel.closed.is_set():
            # Cancelled while we were dialling (hub close, or connection lost).
            channel.shutdown()
            return
        self._send(connection, opened_frame(channel.chan))
        self._spawn(self._pump_channel, connection, channel, name="relay-pump")

    def _pump_channel(self, connection: _Conn, channel: _Channel) -> None:
        """Forward the local terminal websocket to the hub until either dies."""
        sock = channel.sock
        try:
            while connection.alive.is_set() and not channel.closed.is_set():
                frame = recv_frame(sock)
                if frame.opcode == OPCODE_CLOSE:
                    break
                if frame.opcode == OPCODE_PING:
                    channel.pong_local(frame.payload)
                    continue
                if frame.opcode != OPCODE_TEXT:
                    continue
                message = json.loads(frame.payload.decode("utf-8"))
                if not isinstance(message, dict):
                    continue
                self._send(connection, data_frame(channel.chan, message))
        except (WebSocketClosed, OSError, json.JSONDecodeError) as exc:
            log.debug("relay channel %s local read ended: %s", channel.chan, exc)
        except Exception:  # noqa: BLE001 - a pump crash must still close cleanly
            log.exception("relay channel %s pump crashed", channel.chan)
        finally:
            # Local EOF means the session ended (terminate, exit, daemon
            # restart): the hub-side bridge only learns of it from our close.
            connection.forget_channel(channel.chan)
            self._close_channel(connection, channel, notify_hub=True)

    def _handle_data(self, connection: _Conn, frame: dict[str, Any]) -> None:
        chan = str(frame.get("chan"))
        channel = connection.get_channel(chan)
        message = frame.get("message")
        if channel is None or channel.closed.is_set() or not isinstance(message, dict):
            return
        try:
            channel.send_local(message)
        except (OSError, WebSocketClosed) as exc:
            log.debug("relay channel %s local write failed: %s", chan, exc)
            connection.forget_channel(chan)
            self._close_channel(connection, channel, notify_hub=True)

    def _close_channel(
        self, connection: _Conn, channel: _Channel, *, notify_hub: bool
    ) -> None:
        if not channel.mark_closed():
            return
        channel.shutdown()
        if not notify_hub or not connection.alive.is_set():
            return
        try:
            connection.send(close_frame(channel.chan), timeout_s=CLOSE_WRITE_TIMEOUT_S)
        except (OSError, WebSocketClosed, _WriteTimeout) as exc:
            # Deliberately does NOT tear the connection down, unlike _send.
            # This budget is 1s, not 10s: losing a race for the lock that
            # briefly is normal on a busy write path and says nothing about
            # the peer. A genuinely dead socket is the reader's to notice.
            log.debug("relay close frame for %s failed: %s", channel.chan, exc)

    # -- teardown ------------------------------------------------------

    def _teardown(self, connection: _Conn, reason: str) -> None:
        """Idempotent: kill the socket, then every channel riding it."""
        with connection.state_lock:
            if not connection.alive.is_set():
                return
            connection.alive.clear()
        # shutdown() before close() so a thread wedged in sendall is released
        # rather than leaked holding the write lock forever.
        with contextlib.suppress(OSError):
            connection.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            connection.sock.close()
        for channel in connection.drain_channels():
            # The hub socket is already gone; a close frame has nowhere to go.
            if channel.mark_closed():
                channel.shutdown()
        log.info("relay connection torn down: %s", reason)
