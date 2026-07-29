"""Hub-side owner of live relay websocket connections, keyed by host_id.

A spoke (drover-harnessd behind NAT) dials the hub and holds one websocket
open. The hub multiplexes every API call and every terminal attach for that
host over that single socket, so many hub threads share one connection: each
connection therefore owns a write lock (all outbound frames hold it), a
pending-request table for req/res correlation, and a channel table for
terminal streams.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import uuid
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


class RelayUnavailable(RuntimeError):
    """No live relay connection can satisfy this call."""


def _error(message: str) -> tuple[int, str]:
    """Mirror ``_proxy_harness_request``'s error convention."""
    return 502, json.dumps({"error": message}, sort_keys=True) + "\n"


class RelayChannel:
    """One terminal attach stream riding a relay connection."""

    def __init__(self, connection: "_Connection", chan: str) -> None:
        self.chan = chan
        self._connection = connection
        self._incoming: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._closed = threading.Event()
        self._close_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def send(self, message: dict[str, Any]) -> None:
        if self._closed.is_set():
            raise RelayUnavailable(f"relay channel closed: {self.chan}")
        if not self._connection.alive.is_set():
            raise RelayUnavailable(f"relay connection lost: {self._connection.host_id}")
        try:
            self._connection.send(data_frame(self.chan, message))
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
                connection.send(close_frame(self.chan))
            except (OSError, WebSocketClosed) as exc:
                log.debug("relay close frame for %s failed: %s", self.chan, exc)

    def _mark_closed(self) -> bool:
        """Flip to closed exactly once; wake a blocked ``recv``."""
        with self._close_lock:
            if self._closed.is_set():
                return False
            self._closed.set()
        self._incoming.put(None)
        return True


class _Connection:
    """One live relay websocket plus everything multiplexed over it."""

    def __init__(self, host_id: str, sock: socket.socket) -> None:
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

    def send(self, payload: dict[str, Any]) -> None:
        with self.write_lock:
            send_json(self.sock, payload)

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
        connection = _Connection(host_id, sock)
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
        connection = self._live(host_id)
        if connection is None:
            return _error(f"relay host not connected: {host_id}")
        request_id = uuid.uuid4().hex
        waiter: queue.Queue[tuple[int, str]] = queue.Queue(maxsize=1)
        if not connection.register(request_id, waiter):
            return _error(f"relay host not connected: {host_id}")
        try:
            connection.send(req_frame(request_id, method, path, body))
        except (OSError, WebSocketClosed) as exc:
            connection.forget(request_id)
            self._teardown(connection, f"request send failed: {exc}")
            return _error(f"relay request failed: {exc}")
        try:
            return waiter.get(timeout=timeout_s)
        except queue.Empty:
            connection.forget(request_id)
            return _error(f"relay request to {host_id} timed out after {timeout_s}s")

    # -- channels ------------------------------------------------------

    def open_channel(
        self, host_id: str, path: str, timeout_s: float = 10
    ) -> RelayChannel:
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
            connection.send(open_frame(chan, path))
        except (OSError, WebSocketClosed) as exc:
            connection.forget(key)
            connection.forget_channel(chan)
            self._teardown(connection, f"channel open send failed: {exc}")
            raise RelayUnavailable(f"relay channel open failed: {exc}") from exc
        try:
            kind, detail = waiter.get(timeout=timeout_s)
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
                # Same behaviour as ``recv_json``, but the pong takes the
                # write lock: hub threads share this socket.
                with connection.write_lock:
                    send_frame(connection.sock, OPCODE_PONG, frame.payload)
                continue
            if frame.opcode != OPCODE_TEXT:
                continue  # pongs and anything else we do not speak
            payload = json.loads(frame.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                continue
            self._dispatch(connection, parse_frame(payload))

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
                channel._incoming.put(frame.get("message"))
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
                with connection.write_lock:
                    send_frame(connection.sock, OPCODE_PING, b"hb")
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
