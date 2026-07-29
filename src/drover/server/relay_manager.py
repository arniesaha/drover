"""Hub-side owner of live relay websocket connections, keyed by host_id.

A spoke (drover-harnessd behind NAT) dials the hub and holds one websocket
open. The hub multiplexes every API call and every terminal attach for that
host over that single socket, so many hub threads share one connection: each
connection therefore owns a write lock (all outbound frames hold it), a
pending-request table for req/res correlation, and a channel table for
terminal streams.

Every acquisition of that write lock is bounded. A blocking ``sendall`` to a
peer that has stopped draining never returns on its own, so an unbounded
acquire would let one wedged writer swallow every caller's timeout and stall
the reader along with them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

from drover.server.harness.relay_protocol import (
    RelayProtocolError,
    close_frame,
    data_frame,
    open_frame,
    parse_frame,
    req_frame,
)
from drover.server.harness.websocket import (
    OPCODE_CLOSE,
    OPCODE_PING,
    OPCODE_PONG,
    OPCODE_TEXT,
    WebSocketClosed,
    recv_frame,
    send_frame,
    send_json,
)

log = logging.getLogger("drover.relay")

PING_INTERVAL_S = 20.0
# Budget for grabbing the write lock when the caller has no deadline of its own.
WRITE_TIMEOUT_S = 10.0
# The reader's budget. It skips the pong rather than stall: a dropped pong is
# harmless, a stalled reader is not - it would sit on frames already buffered.
PONG_WRITE_TIMEOUT_S = 0.5
# Best effort only; closing a channel must never hang on a busy write path.
CLOSE_WRITE_TIMEOUT_S = 1.0
# Per-channel inbound backlog before the oldest messages are dropped.
CHANNEL_QUEUE_MAX = 1024


class RelayUnavailable(RuntimeError):
    """No live relay connection can satisfy this call."""


class _WriteTimeout(RuntimeError):
    """The connection's write path did not free up in time (internal)."""


def _error(message: str) -> tuple[int, str]:
    """Mirror ``_proxy_harness_request``'s error convention."""
    return 502, json.dumps({"error": message}, sort_keys=True) + "\n"


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


class RelayChannel:
    """One terminal attach stream riding a relay connection."""

    def __init__(self, connection: "_Connection", chan: str) -> None:
        self.chan = chan
        self._connection = connection
        self._incoming: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=CHANNEL_QUEUE_MAX
        )
        self._dropped = 0
        self._closed = threading.Event()
        self._close_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def send(self, message: dict[str, Any], timeout_s: float = WRITE_TIMEOUT_S) -> None:
        if self._closed.is_set():
            raise RelayUnavailable(f"relay channel closed: {self.chan}")
        connection = self._connection
        if not connection.alive.is_set():
            raise RelayUnavailable(f"relay connection lost: {connection.host_id}")
        try:
            connection.send(data_frame(self.chan, message), timeout_s=timeout_s)
        except _WriteTimeout as exc:
            # The write path is wedged mid-frame; this connection can never
            # resume cleanly, so drop it rather than leave a zombie behind.
            connection.teardown(str(exc))
            raise RelayUnavailable(str(exc)) from exc
        except (OSError, WebSocketClosed) as exc:
            raise RelayUnavailable(f"relay channel send failed: {exc}") from exc

    def recv(self, timeout_s: float) -> dict[str, Any] | None:
        """Next message from the spoke, or ``None`` on timeout/close."""
        try:
            return self._incoming.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def close(self) -> None:
        """Idempotent: unregister, then best-effort tell the spoke."""
        if not self._mark_closed():
            return
        connection = self._connection
        connection.forget_channel(self.chan)
        if connection.alive.is_set():
            try:
                connection.send(close_frame(self.chan), timeout_s=CLOSE_WRITE_TIMEOUT_S)
            except (OSError, WebSocketClosed, _WriteTimeout) as exc:
                log.debug("relay close frame for %s failed: %s", self.chan, exc)

    def _offer(self, item: dict[str, Any] | None) -> None:
        """Enqueue inbound data, dropping the oldest message when full.

        Terminal streams are bursty and a stalled consumer must not grow the
        hub without bound. Dropping the oldest (rather than closing the
        channel) keeps a working session alive through a transient burst and
        leaves the newest output - the current screen state - intact.
        """
        while True:
            try:
                self._incoming.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._incoming.get_nowait()
                except queue.Empty:  # pragma: no cover - drained concurrently
                    pass
                self._dropped += 1
                if self._dropped == 1 or self._dropped % 1000 == 0:
                    log.warning(
                        "relay channel %s dropped %d message(s): consumer behind",
                        self.chan,
                        self._dropped,
                    )

    def _mark_closed(self) -> bool:
        """Flip to closed exactly once; wake a blocked ``recv``."""
        with self._close_lock:
            if self._closed.is_set():
                return False
            self._closed.set()
        self._offer(None)
        return True


class _Connection:
    """One live relay websocket plus everything multiplexed over it."""

    def __init__(self, manager: "RelayManager", host_id: str, sock: socket.socket):
        self.manager = manager
        self.host_id = host_id
        self.sock = sock
        self.write_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.pending: dict[str, queue.Queue[Any]] = {}
        self.channels: dict[str, RelayChannel] = {}
        self.alive = threading.Event()
        self.alive.set()
        # Inverse of ``alive``, so the ping thread can wait with a timeout and
        # still wake immediately on teardown.
        self.dead = threading.Event()

    @contextlib.contextmanager
    def write_access(self, timeout_s: float) -> Iterator[None]:
        """Hold the write lock, or give up. Never blocks forever."""
        if not self.write_lock.acquire(timeout=max(timeout_s, 0.0)):
            raise _WriteTimeout(f"relay write path wedged for {self.host_id}")
        try:
            yield
        finally:
            self.write_lock.release()

    def send(self, payload: dict[str, Any], timeout_s: float = WRITE_TIMEOUT_S) -> None:
        with self.write_access(timeout_s):
            send_json(self.sock, payload)

    def send_control(
        self, opcode: int, payload: bytes = b"", timeout_s: float = WRITE_TIMEOUT_S
    ) -> None:
        with self.write_access(timeout_s):
            send_frame(self.sock, opcode, payload)

    def teardown(self, reason: str) -> None:
        self.manager._teardown(self, reason)

    def register(self, key: str, waiter: queue.Queue[Any]) -> bool:
        with self.state_lock:
            if self.dead.is_set():
                return False
            self.pending[key] = waiter
            return True

    def forget(self, key: str) -> None:
        with self.state_lock:
            self.pending.pop(key, None)

    def resolve(self, key: str, result: Any) -> None:
        with self.state_lock:
            waiter = self.pending.pop(key, None)
        if waiter is None:
            return
        try:
            waiter.put_nowait(result)
        except queue.Full:  # pragma: no cover - duplicate reply from spoke
            log.warning("duplicate relay reply for %s on %s", key, self.host_id)

    def add_channel(self, channel: RelayChannel) -> bool:
        with self.state_lock:
            if self.dead.is_set():
                return False
            self.channels[channel.chan] = channel
            return True

    def get_channel(self, chan: str) -> RelayChannel | None:
        with self.state_lock:
            return self.channels.get(chan)

    def forget_channel(self, chan: str) -> RelayChannel | None:
        with self.state_lock:
            return self.channels.pop(chan, None)


class RelayManager:
    """Registry of live relay connections, one per spoke host_id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: dict[str, _Connection] = {}

    # -- lifecycle -----------------------------------------------------

    def attach(self, host_id: str, sock: socket.socket) -> None:
        """Take ownership of an already-upgraded server-role socket.

        Newest wins: a reconnecting spoke must never be blocked by its own
        zombie connection, so any prior connection is torn down.
        """
        connection = _Connection(self, host_id, sock)
        with self._lock:
            previous = self._connections.get(host_id)
            self._connections[host_id] = connection
        if previous is not None:
            self._teardown(previous, "replaced by a newer relay connection")
        threading.Thread(
            target=self._read_forever,
            args=(connection,),
            name=f"relay-reader-{host_id}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._ping_forever,
            args=(connection,),
            name=f"relay-ping-{host_id}",
            daemon=True,
        ).start()
        log.info("relay connection attached for host %s", host_id)

    def is_live(self, host_id: str) -> bool:
        return self._live(host_id) is not None

    def live_host_ids(self) -> set[str]:
        with self._lock:
            items = list(self._connections.items())
        return {host_id for host_id, conn in items if conn.alive.is_set()}

    # -- request/response ----------------------------------------------

    def request(
        self,
        host_id: str,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        timeout_s: float = 15,
    ) -> tuple[int, str]:
        deadline = time.monotonic() + timeout_s
        connection = self._live(host_id)
        if connection is None:
            return _error(f"relay host not connected: {host_id}")
        request_id = uuid.uuid4().hex
        waiter: queue.Queue[tuple[int, str]] = queue.Queue(maxsize=1)
        if not connection.register(request_id, waiter):
            return _error(f"relay host not connected: {host_id}")
        try:
            connection.send(
                req_frame(request_id, method, path, body),
                timeout_s=_remaining(deadline),
            )
        except _WriteTimeout as exc:
            connection.forget(request_id)
            self._teardown(connection, str(exc))
            return _error(f"{exc}; connection dropped")
        except (OSError, WebSocketClosed) as exc:
            connection.forget(request_id)
            self._teardown(connection, f"request send failed: {exc}")
            return _error(f"relay request failed: {exc}")
        try:
            return waiter.get(timeout=_remaining(deadline))
        except queue.Empty:
            connection.forget(request_id)
            return _error(f"relay request to {host_id} timed out after {timeout_s}s")

    # -- channels ------------------------------------------------------

    def open_channel(
        self, host_id: str, path: str, timeout_s: float = 10
    ) -> RelayChannel:
        deadline = time.monotonic() + timeout_s
        connection = self._live(host_id)
        if connection is None:
            raise RelayUnavailable(f"relay host not connected: {host_id}")
        chan = uuid.uuid4().hex
        key = f"open:{chan}"
        channel = RelayChannel(connection, chan)
        waiter: queue.Queue[tuple[Any, Any]] = queue.Queue(maxsize=1)
        # Register the channel before sending ``open``: the spoke may push
        # ``data`` immediately behind its ``opened``, and the reader thread
        # can dispatch it before this thread wakes up.
        if not (connection.register(key, waiter) and connection.add_channel(channel)):
            connection.forget(key)
            raise RelayUnavailable(f"relay connection lost: {host_id}")
        try:
            connection.send(open_frame(chan, path), timeout_s=_remaining(deadline))
        except _WriteTimeout as exc:
            connection.forget(key)
            connection.forget_channel(chan)
            self._teardown(connection, str(exc))
            raise RelayUnavailable(f"{exc}; connection dropped") from exc
        except (OSError, WebSocketClosed) as exc:
            connection.forget(key)
            connection.forget_channel(chan)
            self._teardown(connection, f"channel open send failed: {exc}")
            raise RelayUnavailable(f"relay channel open failed: {exc}") from exc
        try:
            kind, detail = waiter.get(timeout=_remaining(deadline))
        except queue.Empty:
            connection.forget(key)
            connection.forget_channel(chan)
            channel._mark_closed()
            raise RelayUnavailable(
                f"relay channel open to {host_id} timed out after {timeout_s}s"
            ) from None
        if kind != "opened":
            connection.forget_channel(chan)
            channel._mark_closed()
            raise RelayUnavailable(str(detail) if detail else "relay channel refused")
        return channel

    # -- internals -----------------------------------------------------

    def _live(self, host_id: str) -> _Connection | None:
        with self._lock:
            connection = self._connections.get(host_id)
        if connection is None or not connection.alive.is_set():
            return None
        return connection

    def _read_forever(self, connection: _Connection) -> None:
        try:
            self._read_loop(connection)
        except (
            WebSocketClosed,
            OSError,
            RelayProtocolError,
            json.JSONDecodeError,
        ) as exc:
            log.info("relay reader for %s stopped: %s", connection.host_id, exc)
        except Exception:  # noqa: BLE001 - a reader crash must still tear down
            log.exception("relay reader for %s crashed", connection.host_id)
        finally:
            self._teardown(connection, "relay reader stopped")

    def _read_loop(self, connection: _Connection) -> None:
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

    def _pong(self, connection: _Connection, payload: bytes) -> None:
        """Answer a ping under the write lock, but never wait long for it.

        The pong must hold the lock - hub threads share this socket, and pong
        bytes landing mid-frame desync the stream. It must also never block
        the reader, which would leave already-buffered ``res`` frames
        undispatched while the write path is busy.
        """
        try:
            connection.send_control(
                OPCODE_PONG, payload, timeout_s=PONG_WRITE_TIMEOUT_S
            )
        except _WriteTimeout:
            log.warning(
                "relay pong for %s dropped: write path busy", connection.host_id
            )

    def _dispatch(self, connection: _Connection, frame: dict[str, Any]) -> None:
        kind = frame["kind"]
        if kind == "res":
            status = frame.get("status")
            body = frame.get("body")
            connection.resolve(
                str(frame.get("id")),
                (int(status) if isinstance(status, int) else 502, str(body or "")),
            )
        elif kind == "data":
            channel = connection.get_channel(str(frame.get("chan")))
            if channel is not None and not channel.closed:
                channel._offer(frame.get("message"))
        elif kind == "opened":
            connection.resolve(f"open:{frame.get('chan')}", ("opened", None))
        elif kind == "open_error":
            connection.resolve(
                f"open:{frame.get('chan')}", ("open_error", frame.get("error"))
            )
        elif kind == "close":
            channel = connection.forget_channel(str(frame.get("chan")))
            if channel is not None:
                channel._mark_closed()
        elif kind == "hello":
            log.debug("relay hello on live connection for %s", connection.host_id)
        else:  # pragma: no cover - a spoke should never originate these
            log.warning(
                "unexpected relay frame kind from %s: %s", connection.host_id, kind
            )

    def _ping_forever(self, connection: _Connection) -> None:
        """Turn a silently-dead TCP path into a detected disconnect."""
        while not connection.dead.wait(PING_INTERVAL_S):
            try:
                connection.send_control(OPCODE_PING, b"hb")
            except _WriteTimeout as exc:
                self._teardown(connection, str(exc))
                return
            except (OSError, WebSocketClosed) as exc:
                self._teardown(connection, f"ping failed: {exc}")
                return

    def _teardown(self, connection: _Connection, reason: str) -> None:
        """Idempotent: flip presence, then fail everything riding this socket."""
        with connection.state_lock:
            if connection.dead.is_set():
                return
            connection.dead.set()
            connection.alive.clear()
            pending = list(connection.pending.values())
            connection.pending.clear()
            channels = list(connection.channels.values())
            connection.channels.clear()
        # Drop the registry entry before waking anyone, so a caller that sees
        # its (502, ...) also sees ``is_live`` false.
        with self._lock:
            if self._connections.get(connection.host_id) is connection:
                del self._connections[connection.host_id]
        # shutdown() before close() so a thread wedged in sendall is released
        # rather than leaked holding the write lock forever.
        try:
            connection.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.sock.close()
        except OSError:
            pass
        lost = _error("relay connection lost")
        for waiter in pending:
            try:
                waiter.put_nowait(lost)
            except queue.Full:  # pragma: no cover - already resolved
                pass
        for channel in channels:
            channel._mark_closed()
        log.info("relay connection for %s torn down: %s", connection.host_id, reason)
