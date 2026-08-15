"""drover-server HTTP entry point: routing, auth gate, and the WS terminal proxy.

Business logic lives on MetricsCollector in drover.server.metrics; this module
is the thin HTTP shim around it.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import json
import logging
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, unquote_to_bytes, urlparse

from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.model_catalog.models import MAX_ID_LENGTH
from drover.server.harness.relay_protocol import RelayProtocolError, parse_frame
from drover.server.harness.websocket import (
    OPCODE_CLOSE,
    OPCODE_PING,
    OPCODE_PONG,
    OPCODE_TEXT,
    WebSocketClosed,
    accept_key,
    client_handshake,
    client_send_json,
    recv_frame,
    recv_json,
    send_close,
    send_frame,
    send_json,
)
from drover.server.harness.relay_protocol import RELAY_CONTROL_FRAME_BYTES
from drover.server.web.auth import (
    DISABLED,
    AuthSettings,
    bearer_credential,
    request_authorized,
    session_cookie_value,
    token_matches,
)
from drover.server.web.pairing import PairingCodes, ThrottledSource, UnknownCode
from drover.server.web.ui import load_page

if TYPE_CHECKING:
    from drover.server.harness.models import HarnessHost
    from drover.server.metrics import MetricsCollector
    from drover.server.relay_manager import RelayManager

log = logging.getLogger("drover.metrics")

# /auth/pair is the one public write in the system: a device that has no
# credential yet is exactly the caller it exists for. It is hardened inside
# _redeem_pairing_code (single-use codes, TTL, per-source throttle) rather
# than by this gate.
_PUBLIC_PATHS = {"/healthz", "/readyz", "/auth/login", "/auth/pair", "/harness/probe"}

# Total budget for a spoke to send its hello after the 101, across every
# frame it sends -- not per recv. See _accept_relay_websocket.
RELAY_HELLO_TIMEOUT_S = 10.0
# Mirror records buffered for one terminal attach before the newest are
# dropped. Generous, because the worker below amortizes a whole backlog into a
# single DuckDB window: filling this means the database has been unavailable
# for a long time, not that a burst arrived.
MIRROR_QUEUE_MAX = 2048
# Cap on one batched write, so a huge backlog is drained in several bounded
# windows rather than one that holds the connect lock for seconds.
MIRROR_BATCH_MAX = 128
MIRROR_WRITE_ATTEMPTS = 3
# Teardown drain budget for a closing relay terminal channel. Both bounds are
# safety rails, not the expected path: the drain stops as soon as the channel
# has nothing buffered, which is immediately in the common case. They exist
# because teardown runs on a request-handler thread that a still-chattering
# peer must not be able to hold open.
MIRROR_DRAIN_MAX = 256
MIRROR_DRAIN_SECONDS = 2.0
_MESSAGE_PAGE_DEFAULT = 200
_MESSAGE_PAGE_MAX = 500
_GZIP_MIN_BYTES = 1024


@dataclass(frozen=True)
class MessagePageQuery:
    after_seq: int | None
    before_seq: int | None
    through_seq: int | None
    limit: int | None


def _parse_optional_nonnegative_int(
    params: dict[str, list[str]], name: str
) -> int | None:
    values = params.get(name)
    if not values:
        return None
    if len(values) != 1:
        raise ValueError(f"{name} must appear once")
    try:
        value = int(values[0])
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value


def _archived_limit_kwargs(params: dict[str, list[str]]) -> dict[str, int]:
    """Read ``?archived=N`` for a fleet render, or nothing if absent.

    Returning an empty dict rather than a default keeps "caller said nothing"
    distinct from "caller asked for the same number the default happens to
    be" -- only the former may be served from the shared render cache.

    A malformed or out-of-range value is clamped rather than rejected: this
    sits on the 5s poll path, and failing a whole fleet render over a stray
    query string would turn a cosmetic mistake into an outage.
    """
    from drover.server.metrics import MAX_ARCHIVED_SESSION_LIMIT

    raw = params.get("archived")
    if not raw:
        return {}
    try:
        value = int(raw[0])
    except (TypeError, ValueError):
        return {}
    return {"archived_limit": max(0, min(value, MAX_ARCHIVED_SESSION_LIMIT))}


def _parse_message_page_query(params: dict[str, list[str]]) -> MessagePageQuery:
    query = MessagePageQuery(
        after_seq=_parse_optional_nonnegative_int(params, "after_seq"),
        before_seq=_parse_optional_nonnegative_int(params, "before_seq"),
        through_seq=_parse_optional_nonnegative_int(params, "through_seq"),
        limit=_parse_optional_nonnegative_int(params, "limit"),
    )
    if query.after_seq is not None and query.before_seq is not None:
        raise ValueError("after_seq and before_seq are mutually exclusive")
    if query.through_seq is not None and query.after_seq is None:
        raise ValueError("through_seq requires after_seq")
    if (
        query.through_seq is not None
        and query.after_seq is not None
        and query.through_seq < query.after_seq
    ):
        raise ValueError("through_seq must not precede after_seq")
    if query.limit == 0 or (
        query.limit is not None and query.limit > _MESSAGE_PAGE_MAX
    ):
        raise ValueError(f"limit must be between 1 and {_MESSAGE_PAGE_MAX}")
    return query


_COCKPIT_QUERY_FIELDS = frozenset(
    {
        "days",
        "host_id",
        "harness",
        "provider",
        "model",
        "project_key",
        "limit",
        "project_cursor",
        "harness_cursor",
        "host_cursor",
        "model_cursor",
    }
)


def _parse_cockpit_query(query: str):
    """Parse only the bounded, documented cockpit filter surface."""
    from drover.server.cockpit.analytics import AnalyticsFilters

    params = parse_qs(query, keep_blank_values=True)
    unknown = sorted(set(params) - _COCKPIT_QUERY_FIELDS)
    if unknown:
        raise ValueError(f"unsupported query field: {unknown[0]}")
    values: dict[str, Any] = {}
    for name, entries in params.items():
        if len(entries) != 1:
            raise ValueError(f"{name} must appear once")
        value = entries[0].strip()
        if not value:
            raise ValueError(f"{name} must not be empty")
        max_length = 2048 if name.endswith("_cursor") else 256
        if len(value) > max_length:
            raise ValueError(f"{name} is too long")
        values[name] = value
    for numeric in ("days", "limit"):
        if numeric not in values:
            continue
        try:
            values[numeric] = int(values[numeric])
        except ValueError as exc:
            raise ValueError(f"{numeric} must be an integer") from exc
    return AnalyticsFilters(**values)


_INSIGHT_QUERY_FIELDS = frozenset(
    {
        "state",
        "severity",
        "confidence",
        "analyzer_class",
        "host",
        "harness",
        "target_type",
        "target_id",
        "cursor",
        "limit",
    }
)


def _parse_insight_query(query: str):
    from drover.server.advisory.service import InsightFilters

    params = parse_qs(query, keep_blank_values=True)
    unknown = sorted(set(params) - _INSIGHT_QUERY_FIELDS)
    if unknown:
        raise ValueError(f"unsupported query field: {unknown[0]}")
    values: dict[str, Any] = {}
    for name, entries in params.items():
        if len(entries) != 1:
            raise ValueError(f"{name} must appear once")
        value = entries[0].strip()
        if not value:
            raise ValueError(f"{name} must not be empty")
        if len(value) > 512:
            raise ValueError(f"{name} is too long")
        values[name] = value
    if "limit" in values:
        try:
            values["limit"] = int(values["limit"])
        except ValueError as exc:
            raise ValueError("limit must be an integer") from exc
    return InsightFilters(**values)


def _parse_insight_route(path: str) -> tuple[str, str | None] | None:
    parts = path.strip("/").split("/")
    if len(parts) not in {2, 3} or parts[0] != "insights":
        return None
    finding_id = unquote(parts[1])
    action = parts[2] if len(parts) == 3 else None
    return finding_id, action


def _harness_event_record(session_id: str, message: object) -> dict[str, Any] | None:
    """Extract the mirrorable event out of a terminal message, or ``None``.

    Pure and cheap on purpose: terminal forwarding threads run this inline and
    hands the result to a worker, so nothing that touches the database may
    happen here.
    """
    from drover.server.metrics import _optional_str, _parse_event_timestamp

    if not isinstance(message, dict) or message.get("type") != "event":
        return None
    event = message.get("event")
    if not isinstance(event, dict):
        return None
    event_id = str(event.get("event_id") or "").strip()
    event_type = str(event.get("event_type") or "").strip()
    if not event_id or not event_type:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return {
        "event_id": event_id,
        "session_id": session_id,
        "event_type": event_type,
        "payload": payload,
        "normalized_type": _optional_str(event.get("normalized_type")),
        "normalized_source": _optional_str(event.get("normalized_source")),
        "content_preview": _optional_str(event.get("content_preview")),
        "created_at": _parse_event_timestamp(event.get("created_at")),
    }


def _harness_event_frame_record(session_id: str, frame: Any) -> dict[str, Any] | None:
    """Extract a mirror record from a raw direct-terminal WebSocket frame."""
    if frame.opcode != OPCODE_TEXT:
        return None
    try:
        message = json.loads(frame.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _harness_event_record(session_id, message)


def _drain_channel_into_mirror(
    channel: Any, session_id: str, mirror: "_EventMirror"
) -> int:
    """Rescue events still buffered on a terminal channel that is closing.

    harnessd deliberately sends a PTY read's raw ``output`` frame *before*
    the ``terminal.output`` event frame that records it ("echo first — wire
    delivery never waits on registry bookkeeping"), so the trailing event
    frame is the single most likely thing to be in flight at any detach.

    The forwarding loop exits the instant the app side goes away: the reader
    thread sets ``stop`` and ``channel_to_browser`` returns from the top of
    its ``while``, without reading what the channel already has. Closing the
    channel then discarded it -- events harnessd had already generated,
    assigned an ``event_id``, and durably recorded in its own registry, gone
    from the hub's copy forever and with nothing counting them. Detaching
    promptly after output is the normal client behaviour, so this was a
    routine gap in the hub's event log, not a rare race.

    Draining is safe precisely because mirroring is idempotent on
    ``event_id`` (see ``append_events_if_new``): re-offering a record the
    loop already handled costs a skipped insert, never a duplicate.

    Returns the number of mirrorable records rescued.
    """
    rescued = 0
    deadline = time.monotonic() + MIRROR_DRAIN_SECONDS
    while rescued < MIRROR_DRAIN_MAX and time.monotonic() < deadline:
        try:
            message = channel.recv(timeout_s=0.05)
        except Exception:  # noqa: BLE001 - teardown must never raise
            break
        if message is None:
            # Nothing buffered: the backlog is drained (or the channel is
            # already closed). Either way there is nothing left to rescue.
            break
        record = _harness_event_record(session_id, message)
        if record is not None:
            mirror.offer(record)
            rescued += 1
    if rescued:
        log.debug(
            "rescued %d harness event(s) from closing terminal channel for %s",
            rescued,
            session_id,
        )
    return rescued


def _drain_socket_into_mirror(
    upstream: socket.socket, session_id: str, mirror: "_EventMirror"
) -> int:
    """Rescue event frames buffered on a closing direct terminal socket."""
    rescued = 0
    deadline = time.monotonic() + MIRROR_DRAIN_SECONDS
    upstream.settimeout(0.05)
    while rescued < MIRROR_DRAIN_MAX and time.monotonic() < deadline:
        try:
            frame = recv_frame(upstream)
        except Exception:  # noqa: BLE001 - teardown must never raise
            break
        record = _harness_event_frame_record(session_id, frame)
        if record is not None:
            mirror.offer(record)
            rescued += 1
        if frame.opcode == OPCODE_CLOSE:
            break
    if rescued:
        log.debug(
            "rescued %d harness event(s) from closing direct terminal for %s",
            rescued,
            session_id,
        )
    return rescued


class _EventMirror:
    """Batching, off-thread writer for one terminal attach's events.

    Why this exists: terminal forwarding used to mirror inline, and every
    mirror opens DuckDB connections under a process-wide per-path lock that
    is contended with fleet renders and every host's event ingestion. At PTY
    output rates the drain thread could not keep up, the channel's bounded
    inbound queue overflowed, and it dropped the oldest messages -- silently
    losing *terminal output the user was watching* in order to finish a
    database write nobody was waiting on.

    So the two are decoupled: the drain thread only parses and enqueues, and
    this worker does the writing, batching whatever has piled up into one
    connection window. A failed batch stays pending while the attach is live,
    preserving order across transient outages. The queue remains bounded so a
    prolonged registry outage cannot consume unbounded memory or stall terminal
    output; overflow is counted, marked as a transcript gap when possible, and
    logged rather than blocking the interactive stream.
    """

    def __init__(self, registry: HarnessRegistry) -> None:
        self._registry = registry
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=MIRROR_QUEUE_MAX
        )
        self._dropped = 0
        self._drop_lock = threading.Lock()
        self._gap_counts: dict[str, int] = {}
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="terminal-event-mirror", daemon=True
        )
        self._thread.start()

    def offer(self, record: dict[str, Any]) -> None:
        """Never blocks: the caller is holding up a live terminal stream."""
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._dropped += 1
            self._note_dropped([record])
            if self._dropped == 1 or self._dropped % 1000 == 0:
                log.warning(
                    "harness event mirror dropped %d event(s): writer behind",
                    self._dropped,
                )

    def close(self) -> None:
        """Signal the worker to finish its backlog and stop.

        Not joined: the attach is over and the caller is a request handler.
        The worker is a daemon thread draining a bounded queue, so it ends on
        its own -- but the sentinel `put_nowait` is best-effort: if the queue
        is exactly full (the overload state this class exists for), it is
        dropped under `suppress(queue.Full)`. Relying on the sentinel alone
        would then park the worker forever in a blocking `get()` once it
        drains the backlog, leaking a thread and the registry it closes over
        for the life of the process. `_closed` is the guarantee: `_run`'s
        wait is bounded, and once the queue is empty and `_closed` is set it
        stops on its own even with no sentinel in sight.
        """
        self._closed.set()
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)

    def _run(self) -> None:
        pending: tuple[list[dict[str, Any]], bool] | None = None
        while True:
            if pending is None:
                batch, stopping = self._next_batch()
            else:
                batch, stopping = pending
                if self._closed.is_set():
                    self._note_dropped(batch)
                    self._flush_gap_markers()
                    pending = None
                    if stopping:
                        return
                    continue
            if batch:
                if self._write_batch(batch):
                    pending = None
                    self._flush_gap_markers()
                elif self._closed.is_set():
                    self._note_dropped(batch)
                    pending = None
                    self._flush_gap_markers()
                else:
                    # Keep the exact batch and re-offer it on a later cycle.
                    # This preserves ordering and turns a multi-second central
                    # outage into delayed history rather than missing history.
                    pending = (batch, stopping)
                    self._closed.wait(0.25)
                    continue
            if stopping:
                self._flush_gap_markers()
                return

    def _write_batch(self, batch: list[dict[str, Any]]) -> bool:
        for attempt in range(MIRROR_WRITE_ATTEMPTS):
            try:
                self._registry.append_events_if_new(batch)
                return True
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                log.debug(
                    "failed to mirror %d harness event(s), attempt %d/%d: %s",
                    len(batch),
                    attempt + 1,
                    MIRROR_WRITE_ATTEMPTS,
                    exc,
                )
                if attempt + 1 < MIRROR_WRITE_ATTEMPTS:
                    time.sleep(0.05 * (attempt + 1))
        return False

    def _note_dropped(self, records: list[dict[str, Any]]) -> None:
        from drover.server.harness.daemon import record_dropped_events

        record_dropped_events(len(records))
        with self._drop_lock:
            for record in records:
                session_id = str(record.get("session_id") or "").strip()
                if session_id:
                    self._gap_counts[session_id] = (
                        self._gap_counts.get(session_id, 0) + 1
                    )

    def _flush_gap_markers(self) -> None:
        with self._drop_lock:
            pending, self._gap_counts = self._gap_counts, {}
        for session_id, count in pending.items():
            try:
                self._registry.append_event(
                    session_id=session_id,
                    event_type="transcript.gap",
                    payload={"dropped": count},
                    normalized_type="status",
                )
            except Exception as exc:  # noqa: BLE001 - best-effort gap marker
                with self._drop_lock:
                    self._gap_counts[session_id] = (
                        self._gap_counts.get(session_id, 0) + count
                    )
                log.debug(
                    "failed to record terminal mirror gap for %s: %s",
                    session_id,
                    exc,
                )

    def _next_batch(self) -> tuple[list[dict[str, Any]], bool]:
        """Block for one record, then sweep up everything already queued."""
        first = self._next_record()
        if first is None:
            return [], True
        batch = [first]
        stopping = False
        while len(batch) < MIRROR_BATCH_MAX:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                stopping = True
                break
            batch.append(item)
        return batch, stopping

    def _next_record(self) -> dict[str, Any] | None:
        """Block for the next record, waking periodically to check `_closed`.

        A plain `self._queue.get()` would hang forever if `close()`'s
        sentinel was dropped for arriving on a full queue. Polling with a
        timeout costs nothing on the common path -- every real record wakes
        this immediately -- and bounds the worst case to one poll interval
        past the backlog draining.
        """
        while True:
            try:
                return self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._closed.is_set() and self._queue.empty():
                    return None


class _BrowserSocket:
    """The app-facing websocket of a terminal proxy, with a write lock.

    Two threads write here: the forwarding thread sends terminal messages
    while the reader thread answers pings. Unsynchronized, those interleave
    mid-frame and desync the stream to the app permanently -- the exact
    hazard every other socket in this codebase is meticulous about (see the
    relay_client module docstring). It was the one socket left unguarded, and
    the trigger becomes materially more likely once this path runs through
    Tailscale Funnel: a new intermediary that has not been verified not to
    ping. The iOS client itself never sends one.

    Acquiring the lock is bounded without a timeout parameter because the
    socket carries one (``settimeout(0.25)``), so no holder can sit in
    ``sendall`` indefinitely.
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._write_lock = threading.Lock()

    def send_json(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            send_json(self.sock, payload)

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        with self._write_lock:
            send_frame(self.sock, opcode, payload)

    def send_close(self) -> None:
        with self._write_lock:
            send_close(self.sock)

    def recv_json(self) -> dict[str, Any] | None:
        """``recv_json``, except the pong goes through the write lock.

        Reads with ``recv_frame`` rather than delegating, because the module
        level ``recv_json`` answers a ping by writing unlocked -- which is the
        whole bug.
        """
        frame = recv_frame(self.sock)
        if frame.opcode == OPCODE_CLOSE:
            raise WebSocketClosed()
        if frame.opcode == OPCODE_PING:
            self.send_frame(OPCODE_PONG, frame.payload)
            return None
        if frame.opcode != OPCODE_TEXT:
            return None
        loaded = json.loads(frame.payload.decode("utf-8"))
        return loaded if isinstance(loaded, dict) else None


def _derive_awaiting(
    *, event_type: str, payload: dict[str, Any], current: str | None
) -> str | None:
    """Replay StructuredSessionManager.emit()'s awaiting-derivation rules.

    Kept in exact lockstep with drover.server.harness.structured.manager
    .StructuredSessionManager.emit -- any other event type leaves the
    current value unchanged.
    """
    if event_type == "approval_prompt":
        return "approval"
    if event_type in {"approval_response", "user_input"}:
        return None
    if event_type == "status" and payload.get("awaiting") == "input":
        return "input"
    return current


def _parse_host_auth_route(path: str) -> dict[str, str] | None:
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if (
        len(parts) == 6
        and parts[:2] == ["harness", "hosts"]
        and parts[3] == "auth"
        and parts[5] in {"status", "start"}
    ):
        return {
            "host_id": parts[2],
            "harness": parts[4],
            "action": parts[5],
            "method": "GET" if parts[5] == "status" else "POST",
        }
    if (
        len(parts) == 7
        and parts[:2] == ["harness", "hosts"]
        and parts[3] == "auth"
        and parts[5] == "flows"
    ):
        return {
            "host_id": parts[2],
            "harness": parts[4],
            "flow_id": parts[6],
            "action": "flow",
            "method": "GET",
        }
    if (
        len(parts) == 8
        and parts[:2] == ["harness", "hosts"]
        and parts[3] == "auth"
        and parts[5] == "flows"
        and parts[7] == "input"
    ):
        return {
            "host_id": parts[2],
            "harness": parts[4],
            "flow_id": parts[6],
            "action": "input",
            "method": "POST",
        }
    if (
        len(parts) == 8
        and parts[:2] == ["harness", "hosts"]
        and parts[3] == "auth"
        and parts[5] == "flows"
        and parts[7] == "cancel"
    ):
        return {
            "host_id": parts[2],
            "harness": parts[4],
            "flow_id": parts[6],
            "action": "cancel",
            "method": "POST",
        }
    return None


def _parse_model_catalog_route(path: str, query: str) -> tuple[str, str, bool] | None:
    prefix = "/harness/hosts/"
    suffix = "/model-catalog"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    encoded_host_id = path[len(prefix) : -len(suffix)]
    if (
        not encoded_host_id
        or "/" in encoded_host_id
        or len(encoded_host_id) > MAX_ID_LENGTH * 3
        or len(query) > 4_096
    ):
        raise ValueError("invalid model catalog route")
    _validate_percent_encoding(encoded_host_id)
    _validate_percent_encoding(query)
    try:
        host_id = unquote_to_bytes(encoded_host_id).decode("utf-8")
        params = parse_qs(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid model catalog route") from exc
    if not host_id or len(host_id) > MAX_ID_LENGTH:
        raise ValueError("invalid model catalog host")
    if set(params) - {"harness", "refresh"}:
        raise ValueError("unexpected model catalog query parameter")
    harness_values = params.get("harness")
    if (
        harness_values is None
        or len(harness_values) != 1
        or not harness_values[0]
        or len(harness_values[0]) > MAX_ID_LENGTH
    ):
        raise ValueError("harness must appear once")
    refresh_values = params.get("refresh")
    if refresh_values is None:
        refresh = False
    elif len(refresh_values) == 1 and refresh_values[0] in {"0", "1"}:
        refresh = refresh_values[0] == "1"
    else:
        raise ValueError("refresh must appear once and be 0 or 1")
    return host_id, harness_values[0], refresh


def _validate_percent_encoding(value: str) -> None:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if (
            index + 2 >= len(value)
            or value[index + 1] not in "0123456789abcdefABCDEF"
            or value[index + 2] not in "0123456789abcdefABCDEF"
        ):
            raise ValueError("invalid percent encoding")
        index += 3


class _MetricsHandler(BaseHTTPRequestHandler):
    collector: "MetricsCollector"
    auth: AuthSettings = DISABLED
    pairing: PairingCodes | None = None

    def _gate(self, path: str) -> bool:
        """Authorize the request or write the refusal response.

        Returns True when handling may proceed.
        """
        if path in _PUBLIC_PATHS or not self.auth.enabled:
            return True
        if request_authorized(self.auth, self.headers):
            return True
        if path in {"/", "/ui"} or path.startswith("/ui/"):
            self.send_response(302)
            self.send_header("Location", "/auth/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(
                401, "application/json", '{"error": "authentication required"}\n'
            )
        return False

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._gate(path):
            return
        if path == "/healthz":
            # Liveness, and only liveness: the process is running. Everything
            # about the database belongs to /readyz, so that a restart trigger
            # keyed on readiness cannot be defeated by the process being up.
            self._send(200, "text/plain; charset=utf-8", "ok\n")
            return
        if path == "/readyz":
            # Answers 503 when a store this hub serves from can no longer be
            # queried. See drover.server.readiness -- #175, where readiness
            # said ok for hours while every query failed.
            status, body = self.collector.render_readiness(
                include_detail=(
                    not self.auth.enabled or request_authorized(self.auth, self.headers)
                )
            )
            self._send(status, "application/json", body)
            return
        if path == "/auth/login":
            self._send(200, "text/html; charset=utf-8", load_page("login.html"))
            return
        if path == "/auth/credentials":
            self._list_credentials()
            return
        if path in {"/", "/ui"}:
            self._send(200, "text/html; charset=utf-8", load_page("observatory.html"))
            return
        if path == "/ui/harness":
            self._send(200, "text/html; charset=utf-8", load_page("harness.html"))
            return
        if path.startswith("/ui/harness/sessions/"):
            session_id = unquote(path.removeprefix("/ui/harness/sessions/").strip("/"))
            if not session_id:
                self._send(
                    404,
                    "text/html; charset=utf-8",
                    "<!doctype html><title>Harness session not found</title>"
                    "<p>Missing harness session id.</p>"
                    '<p><a href="/ui/harness">Back to sessions</a></p>',
                )
                return
            self._send(
                200, "text/html; charset=utf-8", load_page("harness_terminal.html")
            )
            return
        if path == "/observability":
            self._send(200, "application/json", self.collector.render_json())
            return
        if path in {"/cockpit/overview", "/analytics"}:
            try:
                filters = _parse_cockpit_query(parsed.query)
            except ValueError as exc:
                self._send(
                    400,
                    "application/json",
                    json.dumps({"error": str(exc)}) + "\n",
                )
                return
            if path == "/cockpit/overview":
                status, body = self.collector.render_cockpit_overview_json(filters)
            else:
                status, body = self.collector.render_analytics_json(filters)
            self._send(status, "application/json", body)
            return
        if path == "/insights":
            try:
                filters = _parse_insight_query(parsed.query)
                status, body = self.collector.render_insights_json(filters)
            except ValueError as exc:
                status, body = 400, json.dumps({"error": str(exc)}) + "\n"
            self._send(status, "application/json", body)
            return
        if path == "/insights/content-analysis":
            status, body = self.collector.render_content_analysis_status_json()
            self._send(status, "application/json", body)
            return
        insight_route = _parse_insight_route(path)
        if insight_route and insight_route[1] is None:
            status, body = self.collector.render_insight_json(insight_route[0])
            self._send(status, "application/json", body)
            return
        if path == "/harness":
            self._send(
                200,
                "application/json",
                self.collector.render_harness_json(
                    **_archived_limit_kwargs(parse_qs(parsed.query))
                ),
            )
            return
        if path == "/harness/hosts":
            self._send(
                200,
                "application/json",
                self.collector.render_harness_json(include_sessions=False),
            )
            return
        try:
            model_catalog_route = _parse_model_catalog_route(path, parsed.query)
        except ValueError as exc:
            self._send(
                400,
                "application/json",
                json.dumps({"error": str(exc)}) + "\n",
            )
            return
        if model_catalog_route is not None:
            host_id, harness, refresh = model_catalog_route
            status, body = self.collector.proxy_harness_model_catalog(
                host_id, harness, refresh=refresh
            )
            self._send(status, "application/json", body)
            return
        auth_route = _parse_host_auth_route(path)
        if auth_route and auth_route["method"] == "GET":
            status, body = self.collector.proxy_harness_auth(
                auth_route["host_id"],
                auth_route["harness"],
                auth_route["action"],
                flow_id=auth_route.get("flow_id"),
            )
            self._send(status, "application/json", body)
            return
        if path.startswith("/harness/hosts/") and path.endswith("/native-sessions"):
            host_id = unquote(
                path.removeprefix("/harness/hosts/")
                .removesuffix("/native-sessions")
                .strip("/")
            )
            status, body = self.collector.proxy_harness_native_sessions(
                host_id,
                {
                    key: values[-1]
                    for key, values in parse_qs(parsed.query).items()
                    if values
                },
            )
            self._send(status, "application/json", body)
            return
        if path == "/harness/sessions":
            self._send(
                200,
                "application/json",
                self.collector.render_harness_json(include_hosts=False),
            )
            return
        if path == "/harness/relay":
            self._accept_relay_websocket()
            return
        if path.startswith("/harness/sessions/") and path.endswith("/terminal"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/")
                .removesuffix("/terminal")
                .strip("/")
            )
            self._proxy_terminal_websocket(session_id)
            return
        if path.startswith("/harness/sessions/") and path.endswith(
            "/native-transcript"
        ):
            session_id = unquote(
                path.removeprefix("/harness/sessions/")
                .removesuffix("/native-transcript")
                .strip("/")
            )
            status, body = self.collector.proxy_harness_native_transcript(session_id)
            self._send(status, "application/json", body)
            return
        if path.startswith("/harness/sessions/") and path.endswith("/stream"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/").removesuffix("/stream")
            ).strip("/")
            params = parse_qs(parsed.query)
            raw_after = (params.get("after_seq") or ["0"])[0]
            try:
                after_seq = int(raw_after)
            except ValueError:
                self._send(
                    400,
                    "application/json",
                    '{"error": "after_seq must be an integer"}\n',
                )
                return
            if (self.headers.get("Upgrade") or "").lower() != "websocket":
                self._send(
                    400,
                    "application/json",
                    '{"error": "websocket upgrade required"}\n',
                )
                return
            self._stream_session_messages(session_id, after_seq=after_seq)
            return
        if path.startswith("/harness/sessions/") and path.endswith("/messages"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/").removesuffix("/messages")
            ).strip("/")
            try:
                query = _parse_message_page_query(parse_qs(parsed.query))
            except ValueError as exc:
                self._send(
                    400,
                    "application/json",
                    json.dumps({"error": str(exc)}) + "\n",
                )
                return
            registry = self._harness_registry()
            compatibility_mode = (
                query.limit is None
                and query.before_seq is None
                and query.through_seq is None
            )
            if compatibility_mode:
                events = registry.list_events_after(
                    session_id, query.after_seq if query.after_seq is not None else 0
                )
                payload = {
                    "messages": [event.wire_payload() for event in events],
                    "max_seq": registry.max_event_seq(session_id),
                }
            else:
                page = registry.list_event_page(
                    session_id,
                    after_seq=query.after_seq,
                    before_seq=query.before_seq,
                    through_seq=query.through_seq,
                    limit=query.limit or _MESSAGE_PAGE_DEFAULT,
                )
                payload = {
                    "messages": [event.wire_payload() for event in page.events],
                    "page_min_seq": page.page_min_seq,
                    "page_max_seq": page.page_max_seq,
                    "max_seq": page.max_seq,
                    "has_older": page.has_older,
                    "has_newer": page.has_newer,
                }
            self._send(
                200,
                "application/json",
                json.dumps(payload) + "\n",
                allow_gzip=True,
                route_class="session_messages",
            )
            return
        if path.startswith("/harness/sessions/"):
            session_id = unquote(path.removeprefix("/harness/sessions/").strip("/"))
            status, body = self.collector.render_harness_session_json(session_id)
            self._send(status, "application/json", body)
            return
        if path == "/metrics":
            self._send(
                200,
                "text/plain; version=0.0.4; charset=utf-8",
                self.collector.render_prometheus(),
            )
            return
        self._send(404, "text/plain; charset=utf-8", "not found\n")

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._gate(path):
            return
        if path == "/auth/login":
            self._handle_login()
            return
        if path == "/insights/content-analysis/consent":
            body = self._read_json()
            if body is None:
                self._send(
                    400,
                    "application/json",
                    '{"error": "request body must be a JSON object"}\n',
                )
                return
            status, payload = self.collector.consent_content_analysis(body)
            self._send(status, "application/json", payload)
            return
        if path == "/insights/content-analysis/revoke":
            body = self._read_json()
            if body is None:
                self._send(
                    400,
                    "application/json",
                    '{"error": "request body must be a JSON object"}\n',
                )
                return
            status, payload = self.collector.revoke_content_analysis(body)
            self._send(status, "application/json", payload)
            return
        insight_route = _parse_insight_route(path)
        if insight_route and insight_route[1] in {
            "acknowledge",
            "dismiss",
            "check",
        }:
            body = self._read_json()
            if body is None:
                self._send(
                    400,
                    "application/json",
                    '{"error": "request body must be a JSON object"}\n',
                )
                return
            status, payload = self.collector.act_on_insight(
                insight_route[0], insight_route[1], body
            )
            self._send(status, "application/json", payload)
            return
        if path == "/auth/pair":
            self._redeem_pairing_code()
            return
        if path == "/harness/probe":
            self._probe_join_candidate()
            return
        if path == "/auth/pair-codes":
            self._mint_pairing_code()
            return
        if path == "/harness/events":
            self._ingest_harness_events()
            return
        if path == "/harness/hosts":
            body = self._read_json()
            if body is None:
                self._send(
                    400,
                    "application/json",
                    '{"error": "request body must be valid JSON"}\n',
                )
                return
            status, payload = self.collector.register_harness_host(body)
            self._send(status, "application/json", payload)
            return
        if path.startswith("/harness/hosts/") and path.endswith("/heartbeat"):
            host_id = unquote(
                path.removeprefix("/harness/hosts/")
                .removesuffix("/heartbeat")
                .strip("/")
            )
            if not host_id:
                self._send(400, "application/json", '{"error": "missing host_id"}\n')
                return
            body = self._read_json()
            if body is None:
                self._send(
                    400,
                    "application/json",
                    '{"error": "request body must be valid JSON"}\n',
                )
                return
            body["host_id"] = host_id
            status, payload = self.collector.register_harness_host(body)
            self._send(status, "application/json", payload)
            return
        auth_route = _parse_host_auth_route(path)
        if auth_route and auth_route["method"] == "POST":
            payload = None
            if auth_route["action"] == "input":
                payload = self._read_json()
                if payload is None:
                    self._send(
                        400,
                        "application/json",
                        '{"error": "request body must be valid JSON"}\n',
                    )
                    return
            status, body = self.collector.proxy_harness_auth(
                auth_route["host_id"],
                auth_route["harness"],
                auth_route["action"],
                flow_id=auth_route.get("flow_id"),
                payload=payload,
            )
            self._send(status, "application/json", body)
            return
        if path.startswith("/harness/hosts/") and path.endswith("/sessions"):
            host_id = unquote(
                path.removeprefix("/harness/hosts/")
                .removesuffix("/sessions")
                .strip("/")
            )
            if not host_id:
                self._send(400, "application/json", '{"error": "missing host_id"}\n')
                return
            body = self._read_json()
            if body is None:
                self._send(
                    400,
                    "application/json",
                    '{"error": "request body must be valid JSON"}\n',
                )
                return
            status, payload = self.collector.proxy_create_harness_session(host_id, body)
            self._send(status, "application/json", payload)
            return
        for action in ("turns", "permission", "interrupt"):
            suffix = f"/{action}"
            if path.startswith("/harness/sessions/") and path.endswith(suffix):
                session_id = unquote(
                    path.removeprefix("/harness/sessions/")
                    .removesuffix(suffix)
                    .strip("/")
                )
                if not session_id:
                    self._send(
                        400, "application/json", '{"error": "missing session_id"}\n'
                    )
                    return
                body = self._read_json() if action != "interrupt" else {}
                if body is None:
                    self._send(
                        400,
                        "application/json",
                        '{"error": "request body must be valid JSON"}\n',
                    )
                    return
                status, payload = self.collector.proxy_harness_session_action(
                    session_id, action, body
                )
                self._send(status, "application/json", payload)
                return
        if path.startswith("/harness/sessions/") and path.endswith("/terminate"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/")
                .removesuffix("/terminate")
                .strip("/")
            )
            if not session_id:
                self._send(400, "application/json", '{"error": "missing session_id"}\n')
                return
            status, payload = self.collector.proxy_terminate_harness_session(session_id)
            self._send(status, "application/json", payload)
            return
        if path.startswith("/harness/sessions/") and path.endswith("/continue"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/")
                .removesuffix("/continue")
                .strip("/")
            )
            if not session_id:
                self._send(400, "application/json", '{"error": "missing session_id"}\n')
                return
            body = self._read_json()
            if body is None:
                self._send(
                    400,
                    "application/json",
                    '{"error": "request body must be valid JSON"}\n',
                )
                return
            status, payload = self.collector.continue_harness_session(session_id, body)
            self._send(status, "application/json", payload)
            return
        self._send(404, "text/plain; charset=utf-8", "not found\n")

    def do_PUT(self) -> None:  # noqa: N802 - stdlib method name
        path = urlparse(self.path).path
        if path == "/auth/device/apns":
            self._set_device_apns_registration()
            return
        if not self._gate(path):
            return
        self._send(404, "text/plain; charset=utf-8", "not found\n")

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/auth/device/apns":
            self._clear_device_apns_registration()
            return
        if not self._gate(path):
            return
        if path.startswith("/auth/credentials/"):
            self._revoke_credential(unquote(path.removeprefix("/auth/credentials/")))
            return
        if path == "/insights/content-excerpts":
            status, payload = self.collector.purge_content_excerpts()
            self._send(status, "application/json", payload)
            return
        self._send(404, "text/plain; charset=utf-8", "not found\n")

    def log_message(self, fmt: str, *args: object) -> None:
        log.debug("metrics http: " + fmt, *args)

    def _proxy_terminal_websocket(self, session_id: str) -> None:
        if not session_id:
            self._send(404, "application/json", '{"error": "missing session_id"}\n')
            return
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self._send(
                426,
                "application/json",
                '{"error": "terminal attach requires websocket upgrade"}\n',
            )
            return
        if not self.headers.get("Sec-WebSocket-Key"):
            self._send(
                400,
                "application/json",
                '{"error": "missing Sec-WebSocket-Key"}\n',
            )
            return

        route = self.collector.harness_terminal_route(session_id)
        if route is None:
            self._send(
                404,
                "application/json",
                '{"error": "unknown terminal session or host endpoint"}\n',
            )
            return
        host, path = route

        relay_manager = self.collector.relay_manager
        if relay_manager is not None and relay_manager.is_live(host.host_id):
            self._proxy_terminal_over_relay(
                session_id, host.host_id, path, relay_manager
            )
            return
        if getattr(host, "connection_kind", "direct") == "relay":
            # Same rule as _harness_request: never dial a relay host by URL.
            # Its socket is the only way in, and the default listen address
            # everywhere in this repo is 127.0.0.1:7081 -- so a stray URL on a
            # relay row would attach the user's terminal to the HUB's own
            # harnessd. Unreachable is the safe failure.
            self._send(
                502,
                "application/json",
                json.dumps({"error": f"relay host is not connected: {host.host_id}"})
                + "\n",
            )
            return
        self._proxy_terminal_direct(session_id, host, path)

    def _proxy_terminal_direct(
        self, session_id: str, host: "HarnessHost", path: str
    ) -> None:
        from drover.server.metrics import _harness_endpoint

        endpoint = _harness_endpoint(host)
        if not endpoint:
            self._send(
                404,
                "application/json",
                '{"error": "unknown terminal session or host endpoint"}\n',
            )
            return

        parsed = urlparse(f"{endpoint}{path}")
        upstream_host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        upstream_path = parsed.path or "/"
        upstream: socket.socket | None = None
        try:
            upstream = socket.create_connection((upstream_host, port), timeout=10)
            upstream_headers = (
                {"Authorization": f"Bearer {self.collector.api_token}"}
                if self.collector.api_token
                else None
            )
            client_handshake(
                upstream,
                host=f"{upstream_host}:{port}",
                path=upstream_path,
                headers=upstream_headers,
            )
        except Exception as exc:  # noqa: BLE001
            if upstream is not None:
                upstream.close()
            self._send(
                502,
                "application/json",
                json.dumps({"error": f"harness websocket upstream failed: {exc}"})
                + "\n",
            )
            return

        raw_browser = self._upgrade_browser_websocket()
        if raw_browser is None:
            upstream.close()
            return

        stop = threading.Event()
        raw_browser.settimeout(0.25)
        upstream.settimeout(0.25)
        browser = _BrowserSocket(raw_browser)
        mirror = _EventMirror(self._harness_registry())

        def browser_to_upstream() -> None:
            try:
                while not stop.is_set():
                    try:
                        message = browser.recv_json()
                    except socket.timeout:
                        continue
                    if message is not None:
                        client_send_json(upstream, message)
            except Exception:
                stop.set()

        def upstream_to_browser() -> None:
            try:
                while not stop.is_set():
                    try:
                        frame = recv_frame(upstream)
                    except socket.timeout:
                        continue
                    record = _harness_event_frame_record(session_id, frame)
                    if record is not None:
                        mirror.offer(record)
                    browser.send_frame(frame.opcode, frame.payload)
                    if frame.opcode == OPCODE_CLOSE:
                        stop.set()
                        return
            except Exception:
                stop.set()

        thread = threading.Thread(target=browser_to_upstream, daemon=True)
        try:
            thread.start()
            upstream_to_browser()
        finally:
            stop.set()
            _drain_socket_into_mirror(upstream, session_id, mirror)
            upstream.close()
            mirror.close()

    def _proxy_terminal_over_relay(
        self,
        session_id: str,
        host_id: str,
        path: str,
        relay_manager: "RelayManager",
    ) -> None:
        from drover.server.relay_manager import RelayUnavailable

        try:
            channel = relay_manager.open_channel(host_id, path)
        except RelayUnavailable as exc:
            self._send(
                502,
                "application/json",
                json.dumps({"error": f"harness websocket upstream failed: {exc}"})
                + "\n",
            )
            return

        mirror: _EventMirror | None = None
        try:
            raw_browser = self._upgrade_browser_websocket()
            if raw_browser is None:
                return

            stop = threading.Event()
            raw_browser.settimeout(0.25)
            browser = _BrowserSocket(raw_browser)
            # Off-thread and batched: mirroring inline here is what let a slow
            # DuckDB write turn into dropped terminal output (see _EventMirror).
            mirror = _EventMirror(self._harness_registry())

            def browser_to_channel() -> None:
                try:
                    while not stop.is_set():
                        try:
                            message = browser.recv_json()
                        except socket.timeout:
                            continue
                        if message is not None:
                            channel.send(message)
                except Exception:
                    stop.set()

            def channel_to_browser() -> None:
                try:
                    while not stop.is_set():
                        message = channel.recv(timeout_s=0.25)
                        if message is None:
                            if channel.closed:
                                stop.set()
                                try:
                                    browser.send_close()
                                except Exception:  # noqa: BLE001
                                    pass
                                return
                            continue
                        record = _harness_event_record(session_id, message)
                        if record is not None:
                            mirror.offer(record)
                        browser.send_json(message)
                except Exception:
                    stop.set()

            thread = threading.Thread(target=browser_to_channel, daemon=True)
            thread.start()
            channel_to_browser()
            stop.set()
        finally:
            # Drain BEFORE closing: whatever the forwarding loop had not read
            # yet is still recoverable until the channel goes away.
            if mirror is not None:
                _drain_channel_into_mirror(channel, session_id, mirror)
            channel.close()
            if mirror is not None:
                mirror.close()

    def _upgrade_browser_websocket(self) -> socket.socket | None:
        """Send the browser-side 101 upgrade and hijack the connection.

        Shared by the direct and relay terminal-proxy flavors. Must only be
        called once the upstream (direct socket or relay channel) is already
        established -- a browser must never see a successful upgrade for a
        session the hub could not actually reach; the caller sends a normal
        HTTP error response instead in that case.
        """
        websocket_key = self.headers.get("Sec-WebSocket-Key")
        if not websocket_key:
            self._send(
                400,
                "application/json",
                '{"error": "missing Sec-WebSocket-Key"}\n',
            )
            return None
        # BaseHTTPRequestHandler defaults to HTTP/1.0; strict WebSocket
        # clients (URLSessionWebSocketTask) reject an "HTTP/1.0 101" status
        # line. Scoped to the upgrade response only — the socket is hijacked
        # after this, so HTTP/1.1 keep-alive semantics never apply.
        self.protocol_version = "HTTP/1.1"
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(websocket_key))
        self.end_headers()
        self.close_connection = True
        return self.connection

    def _accept_relay_websocket(self) -> None:
        """Upgrade a spoke's connection and hand it to RelayManager.

        The Bearer check already ran in ``_gate`` (called at the top of
        ``do_GET``) -- this route is never reachable without it, same as
        every other non-public path.
        """
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self._send(
                426,
                "application/json",
                '{"error": "relay attach requires websocket upgrade"}\n',
            )
            return
        websocket_key = self.headers.get("Sec-WebSocket-Key")
        if not websocket_key:
            self._send(
                400,
                "application/json",
                '{"error": "missing Sec-WebSocket-Key"}\n',
            )
            return

        # See _proxy_terminal_websocket: HTTP/1.1 status line required by
        # strict WebSocket clients; scoped to this hijacked upgrade only.
        self.protocol_version = "HTTP/1.1"
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(websocket_key))
        self.end_headers()
        self.close_connection = True

        sock = self.connection
        # One deadline for the whole handshake, not per recv. settimeout() is
        # per socket operation, and recv_json returns None for a ping, a pong,
        # or any non-text opcode -- so a peer sending a ping every 9 seconds
        # used to loop here forever, pinning a ThreadingHTTPServer thread with
        # no cap on how many. Reachable from the internet by anyone holding
        # the shared token once the funnel is up, and unbounded thread growth
        # takes the whole hub down, not just the relay.
        deadline = time.monotonic() + RELAY_HELLO_TIMEOUT_S
        try:
            frame = None
            while frame is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RelayProtocolError(
                        f"no hello frame within {RELAY_HELLO_TIMEOUT_S}s"
                    )
                sock.settimeout(max(0.1, remaining))
                frame = recv_json(sock, max_frame_bytes=RELAY_CONTROL_FRAME_BYTES)
            parsed = parse_frame(frame)
            if parsed.get("kind") != "hello":
                raise RelayProtocolError(
                    f"expected hello frame, got kind={parsed.get('kind')!r}"
                )
            host_id = str(parsed.get("host_id") or "").strip()
            if not host_id:
                raise RelayProtocolError("hello frame missing host_id")
            raw_capabilities = parsed.get("capabilities") or []
            if not isinstance(raw_capabilities, list) or any(
                not isinstance(item, str) for item in raw_capabilities
            ):
                raise RelayProtocolError("hello capabilities must be a string list")
            capabilities = set(raw_capabilities)
        except (OSError, WebSocketClosed, RelayProtocolError, ValueError) as exc:
            log.info("relay handshake for %s failed: %s", self.client_address, exc)
            sock.close()
            return

        # RelayManager owns a blocking socket -- a leftover read timeout
        # would tear a healthy idle connection down (socket.timeout is an
        # OSError, indistinguishable from a real failure to the reader).
        sock.settimeout(None)

        # The socket must outlive this handler: BaseRequestHandler.finish()
        # (called when this method returns) closes rfile/wfile, and
        # ThreadingHTTPServer's shutdown_request() then calls shutdown()
        # and close() on the *same* socket object RelayManager is about to
        # own. detach() extracts the live fd without closing it and marks
        # this socket object closed, so those later calls become no-ops
        # (shutdown raises EBADF, caught by socketserver; close is a
        # no-op) while the fd itself, rewrapped below, keeps working.
        fd = sock.detach()
        relay_sock = socket.socket(fileno=fd)
        self.collector.relay_manager.attach(
            host_id, relay_sock, capabilities=capabilities
        )

    def _harness_registry(self) -> HarnessRegistry:
        return HarnessRegistry(self.collector.duckdb_path)

    def _stream_session_messages(self, session_id: str, *, after_seq: int = 0) -> None:
        """WS handler: push new structured events for a session as they land.

        Handshakes at 101, then polls the registry roughly once a second,
        emitting each new event (seq order) as its own JSON text frame.
        Breaks cleanly on a client close frame, a socket timeout is just an
        empty poll (not an error), and any other socket/protocol failure
        (abrupt disconnect, reset, etc.) ends the loop without raising.
        """
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send(
                400,
                "application/json",
                '{"error": "missing Sec-WebSocket-Key"}\n',
            )
            return
        # See _proxy_terminal_websocket: HTTP/1.1 status line required by
        # strict WebSocket clients; scoped to this hijacked upgrade only.
        self.protocol_version = "HTTP/1.1"
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(key))
        self.end_headers()
        self.close_connection = True
        sock = self.connection
        sock.settimeout(0.2)
        registry = self._harness_registry()
        last_seq = after_seq
        try:
            while True:
                for event in registry.list_events_after(session_id, last_seq):
                    if event.seq is not None:
                        last_seq = event.seq
                    send_frame(
                        sock,
                        OPCODE_TEXT,
                        json.dumps(event.wire_payload()).encode("utf-8"),
                    )
                try:
                    frame = recv_frame(sock)
                except socket.timeout:
                    time.sleep(0.8)
                    continue
                if frame.opcode == OPCODE_CLOSE:
                    break
                if frame.opcode == OPCODE_PING:
                    send_frame(sock, OPCODE_PONG, frame.payload)
                    continue
                if frame.opcode == OPCODE_PONG:
                    continue
        except (OSError, WebSocketClosed):
            pass

    def _ingest_harness_events(self) -> None:
        """POST /harness/events: bulk ingest from a remote host's EventPusher.

        Idempotent by event_id (skips events already recorded, e.g. one that
        the terminal-websocket mirror already wrote). Malformed bodies get a
        400; a well-formed body always responds 200 with the count of newly
        recorded events, even if some/all were already present.

        On split-DB deployments (central and harnessd each with their own
        DuckDB file) this is the ONLY place central's copy of the session row
        learns about `awaiting`/`last_activity` -- the daemon's own
        StructuredSessionManager.emit() only ever updates its local registry.
        So after recording each new event, replay the same awaiting-
        derivation rules emit() uses, and persist the final result once per
        session touched in this batch (not once per event, to avoid N
        redundant writes for a large batch).
        """
        from drover.server.metrics import _parse_event_timestamp

        body = self._read_json()
        if body is None:
            self._send(
                400,
                "application/json",
                '{"error": "request body must be valid JSON"}\n',
            )
            return
        events = body.get("events")
        if not isinstance(events, list) or not all(
            isinstance(event, dict)
            and event.get("event_id")
            and event.get("session_id")
            and event.get("type")
            for event in events
        ):
            self._send(
                400,
                "application/json",
                '{"error": "events must be a list of objects with '
                'event_id, session_id, and type"}\n',
            )
            return
        registry = self._harness_registry()
        ingested = 0
        # Sort defensively by seq: the derivation rules are order-sensitive
        # (e.g. approval_prompt then approval_response must land as "not
        # awaiting", not the reverse), and a batch is not guaranteed to
        # arrive in seq order.
        ordered = sorted(
            events,
            key=lambda event: (
                event.get("seq") if isinstance(event.get("seq"), int) else 0
            ),
        )
        session_awaiting: dict[str, str | None] = {}
        session_last_activity: dict[str, datetime] = {}
        session_native_ids: dict[str, str] = {}
        touched_sessions: set[str] = set()
        for event in ordered:
            event_id = str(event["event_id"])
            session_id = str(event["session_id"])
            if registry.get_event(event_id) is not None:
                continue
            created_at = _parse_event_timestamp(event.get("ts")) or datetime.now(
                timezone.utc
            )
            registry.append_event(
                session_id=session_id,
                event_type=str(event["type"]),
                payload=event,
                seq=event.get("seq"),
                normalized_source="structured",
                event_id=event_id,
                created_at=created_at,
            )
            ingested += 1
            touched_sessions.add(session_id)
            if session_id not in session_awaiting:
                existing = registry.get_session(session_id)
                session_awaiting[session_id] = existing.awaiting if existing else None
            inner_payload = event.get("payload")
            if isinstance(inner_payload, dict):
                native_session_id = inner_payload.get("native_session_id")
                if isinstance(native_session_id, str) and native_session_id.strip():
                    session_native_ids[session_id] = native_session_id.strip()
            session_awaiting[session_id] = _derive_awaiting(
                event_type=str(event["type"]),
                payload=inner_payload if isinstance(inner_payload, dict) else {},
                current=session_awaiting[session_id],
            )
            latest = session_last_activity.get(session_id)
            if latest is None or created_at > latest:
                session_last_activity[session_id] = created_at
        for session_id in touched_sessions:
            native_session_id = session_native_ids.get(session_id)
            if native_session_id is not None:
                registry.update_session_native_id(session_id, native_session_id)
            registry.update_session_activity(
                session_id,
                awaiting=session_awaiting.get(session_id),
                last_activity=session_last_activity.get(session_id),
            )
        self._send(200, "application/json", json.dumps({"ingested": ingested}) + "\n")

    def _handle_login(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = (
            self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        )
        fields = parse_qs(raw)
        candidate = (fields.get("token") or [""])[0].strip()
        if self.auth.enabled and not token_matches(self.auth, candidate):
            self.send_response(302)
            self.send_header("Location", "/auth/login?error=1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(302)
        self.send_header("Location", "/ui")
        if self.auth.enabled:
            self.send_header("Set-Cookie", session_cookie_value(self.auth))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _pair_source(self) -> str:
        return self.client_address[0] if self.client_address else "unknown"

    def _pairing_ready(self) -> bool:
        if self.pairing is not None and self.auth.credentials is not None:
            return True
        self._send(404, "application/json", '{"error": "pairing is not enabled"}\n')
        return False

    def _probe_join_candidate(self) -> None:
        """Can the hub reach the machine that is joining?

        Gated by an unburned host code rather than a bearer token, because the
        joining machine has no credential yet. Deliberately does not burn the
        code: the installer redeems it immediately afterwards, so consuming it
        here would make a failed probe cost a fresh code.

        This is the only route that makes the server dial an address a caller
        supplied, so the address bounds below are load-bearing.
        """
        import ipaddress
        import socket
        import urllib.error
        import urllib.request

        if not self._pairing_ready():
            return
        body = self._read_json()
        if body is None:
            self._send(
                400,
                "application/json",
                '{"error": "request body must be valid JSON"}\n',
            )
            return
        try:
            entry = self.pairing.peek(
                str(body.get("code") or ""), source=self._pair_source()
            )
        except ThrottledSource:
            self._send(429, "application/json", '{"error": "too many attempts"}\n')
            return
        except UnknownCode:
            self._send(
                410, "application/json", '{"error": "unknown or expired code"}\n'
            )
            return
        if entry.scope != "host":
            self._send(400, "application/json", '{"error": "host code required"}\n')
            return

        target = str(body.get("url") or "")
        parsed = urlparse(target)
        if parsed.scheme != "http" or not parsed.hostname:
            self._send(400, "application/json", '{"error": "http url required"}\n')
            return
        # Only ever dial private space. Without this the route is an
        # unauthenticated request forwarder pointed anywhere the hub can
        # reach. ipaddress.is_private returns False for Tailscale's
        # 100.64.0.0/10 (shared address space, not private), so the CGNAT
        # range is allowed explicitly or every tailnet join would be refused.
        try:
            resolved = socket.gethostbyname(parsed.hostname)
            address = ipaddress.ip_address(resolved)
            in_cgnat = address in ipaddress.ip_network("100.64.0.0/10")
            if not (address.is_private or in_cgnat):
                raise ValueError("public address")
        except (OSError, ValueError):
            self._send(400, "application/json", '{"error": "private url required"}\n')
            return

        reachable = True
        try:
            with urllib.request.urlopen(target, timeout=3):
                pass
        except urllib.error.HTTPError:
            # It answered. The status says nothing about reachability.
            reachable = True
        except Exception:  # noqa: BLE001 - any transport failure means no route
            reachable = False
        self._send(200, "application/json", json.dumps({"reachable": reachable}) + "\n")

    def _mint_pairing_code(self) -> None:
        if not self._pairing_ready():
            return
        body = self._read_json()
        if body is None:
            self._send(
                400,
                "application/json",
                '{"error": "request body must be valid JSON"}\n',
            )
            return
        scope = str(body.get("scope") or "device")
        if scope not in {"device", "host"}:
            self._send(400, "application/json", '{"error": "unknown scope"}\n')
            return
        default_label = "New host" if scope == "host" else "New device"
        label = str(body.get("label") or "").strip() or default_label
        raw_host_id = body.get("host_id")
        host_id = str(raw_host_id).strip() if raw_host_id else None
        entry = self.pairing.mint(scope=scope, label=label, host_id=host_id)
        remaining = max(int(entry.expires_at - time.monotonic()), 0)
        payload = {
            "code": entry.formatted,
            "scope": entry.scope,
            "label": entry.label,
            "expires_in_seconds": remaining,
            "fleet_name": self.auth.credentials.fleet_name,
            "server_id": self.auth.credentials.server_id,
        }
        self._send(201, "application/json", json.dumps(payload, sort_keys=True) + "\n")

    def _redeem_pairing_code(self) -> None:
        if not self._pairing_ready():
            return
        body = self._read_json()
        if body is None:
            self._send(
                400,
                "application/json",
                '{"error": "request body must be valid JSON"}\n',
            )
            return
        try:
            entry = self.pairing.redeem(
                str(body.get("code") or ""), source=self._pair_source()
            )
        except ThrottledSource:
            self._send(
                429, "application/json", '{"error": "too many pairing attempts"}\n'
            )
            return
        except UnknownCode:
            # Deliberately identical for unknown, burned, and expired codes.
            self._send(
                410, "application/json", '{"error": "unknown or expired code"}\n'
            )
            return
        store = self.auth.credentials
        label = str(body.get("device_name") or "").strip() or entry.label
        # Scope comes from the code, never from the body: a device code must
        # not be redeemable into a host credential.
        credential, token = store.issue(
            scope=entry.scope, label=label, host_id=entry.host_id
        )
        payload = {
            "token": token,
            "credential_id": credential.id,
            "scope": credential.scope,
            "server_id": store.server_id,
            "fleet_name": store.fleet_name,
        }
        self._send(201, "application/json", json.dumps(payload, sort_keys=True) + "\n")

    def _list_credentials(self) -> None:
        if not self._pairing_ready():
            return
        payload = {
            "credentials": [
                item.as_public_json() for item in self.auth.credentials.list_all()
            ]
        }
        self._send(200, "application/json", json.dumps(payload, sort_keys=True) + "\n")

    def _revoke_credential(self, credential_id: str) -> None:
        if not self._pairing_ready():
            return
        if self.auth.credentials.revoke(credential_id):
            self._send(204, "application/json", "")
            return
        self._send(404, "application/json", '{"error": "unknown credential"}\n')

    def _device_bearer_credential(self):
        credential = bearer_credential(self.auth, self.headers)
        if credential is None:
            self._send(
                401, "application/json", '{"error": "authentication required"}\n'
            )
            return None
        if credential.scope != "device":
            self._send(
                403, "application/json", '{"error": "device credential required"}\n'
            )
            return None
        return credential

    def _set_device_apns_registration(self) -> None:
        credential = self._device_bearer_credential()
        if credential is None:
            return
        body = self._read_json()
        if body is None:
            self._send(
                400,
                "application/json",
                '{"error": "request body must be a JSON object"}\n',
            )
            return
        raw_token = body.get("token")
        environment = body.get("environment")
        if not isinstance(raw_token, str) or environment not in {
            "sandbox",
            "production",
        }:
            self._send(
                400, "application/json", '{"error": "invalid APNs registration"}\n'
            )
            return
        token = raw_token.strip()
        if not token:
            self._send(
                400, "application/json", '{"error": "invalid APNs registration"}\n'
            )
            return
        self.auth.credentials.set_apns_registration(
            credential.id, token=token, environment=environment
        )
        self._send(204, "application/json", "")

    def _clear_device_apns_registration(self) -> None:
        credential = self._device_bearer_credential()
        if credential is None:
            return
        self.auth.credentials.clear_apns_registration(credential.id)
        self._send(204, "application/json", "")

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _send(
        self,
        status: int,
        content_type: str,
        body: str,
        *,
        allow_gzip: bool = False,
        route_class: str | None = None,
    ) -> None:
        started = time.monotonic()
        payload = body.encode("utf-8")
        uncompressed_bytes = len(payload)
        headers = getattr(self, "headers", {})
        accepts_gzip = "gzip" in {
            encoding.split(";", 1)[0].strip().lower()
            for encoding in (headers.get("Accept-Encoding") or "").split(",")
        }
        compressed = allow_gzip and accepts_gzip and len(payload) >= _GZIP_MIN_BYTES
        if compressed:
            payload = gzip.compress(payload)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if allow_gzip:
            self.send_header("Vary", "Accept-Encoding")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(payload)))
        if self.path.startswith(("/ui/harness", "/harness")):
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        try:
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            log.info(
                "client disconnected while sending %s bytes for %s",
                len(payload),
                route_class or self.path,
            )
        if route_class:
            log.info(
                "http_response route_class=%s status=%d uncompressed_bytes=%d "
                "transferred_bytes=%d elapsed_ms=%.3f",
                route_class,
                status,
                uncompressed_bytes,
                len(payload),
                (time.monotonic() - started) * 1000,
            )


def start_metrics_server(
    *,
    host: str,
    port: int,
    collector: "MetricsCollector",
    auth: AuthSettings | None = None,
    pairing: PairingCodes | None = None,
) -> ThreadingHTTPServer:
    """Start the Drover metrics HTTP server in a daemon thread."""
    from drover.server.relay_manager import RelayManager

    collector.relay_manager = RelayManager()
    handler = type(
        "DroverMetricsHandler",
        (_MetricsHandler,),
        {"collector": collector, "auth": auth or DISABLED, "pairing": pairing},
    )
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(
        target=server.serve_forever, name="drover-metrics", daemon=True
    )
    thread.start()
    return server
