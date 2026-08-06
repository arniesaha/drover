"""Redis Streams adapter for Drover derived-job queues.

The pure-Python :mod:`drover.server.jobs.streams` model is the executable
contract. This module is the production-shaped adapter for the same contract:
one stream, one consumer group, at-least-once delivery, ACK only after durable
effects, due-aware pending recovery, DLQ streams, replay, and backpressure
snapshots.

Redis is still execution coordination only. The DuckDB ledger / serving tables
remain the durable source of truth, so this adapter is safe to enable
incrementally around existing workers.
"""

from __future__ import annotations

import json
import logging
import re
import time as _time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from drover.server.jobs.streams import DeadLetter, Delivery, PendingEntry

log = logging.getLogger("drover.jobs.redis_streams")

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{6,}"),
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


@dataclass(frozen=True)
class RedisJobStreamConfig:
    """Runtime settings for one Redis-backed job stream."""

    stream: str
    group: str = "workers"
    max_deliveries: int = 5
    visibility_timeout_ms: int = 60_000
    maxlen: int = 100_000
    high_water: int = 1_000


class RedisJobStream:
    """Redis-backed implementation of the ``JobStream`` contract.

    The Redis client is intentionally injected. Production passes a
    ``redis.Redis`` instance; tests pass a command-surface fake. All returned
    values use the same dataclasses as the reference model.
    """

    def __init__(
        self,
        client: Any,
        config: RedisJobStreamConfig,
        *,
        ensure_group: bool = True,
    ) -> None:
        if config.max_deliveries < 1:
            raise ValueError("max_deliveries must be >= 1")
        self._client = client
        self.config = config
        self.name = config.stream
        self.group = config.group
        self.dead_stream = f"{config.stream}:dead"
        self.error_hash = f"{config.stream}:errors"
        self.deferred_zset = f"{config.stream}:deferred"
        if ensure_group:
            self._ensure_group()

    @classmethod
    def from_url(cls, url: str, config: RedisJobStreamConfig) -> "RedisJobStream":
        """Build a stream from a Redis URL.

        Imports ``redis`` lazily so the base Drover install stays dependency-free.
        Install with ``drover[redis]`` when enabling this adapter.
        """
        try:
            import redis  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on env extras
            raise RuntimeError(
                "install drover[redis] to use Redis job streams"
            ) from exc
        client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5)
        client.ping()
        return cls(client, config)

    # -- producer side -------------------------------------------------

    def add(self, fields: Dict[str, Any]) -> str:
        payload = _encode_fields(fields)
        kwargs: dict[str, Any] = {}
        if self.config.maxlen:
            kwargs.update({"maxlen": self.config.maxlen, "approximate": True})
        return _to_str(self._client.xadd(self.name, payload, **kwargs))

    # -- consumer side -------------------------------------------------

    def read_group(self, consumer: str, count: int = 1) -> List[Delivery]:
        try:
            rows = self._client.xreadgroup(
                self.group,
                consumer,
                {self.name: ">"},
                count=count,
                block=1000,
            )
        except TimeoutError:
            return []
        except Exception as exc:  # noqa: BLE001 - redis-py timeout type is optional
            if "Timeout reading from socket" in str(exc):
                return []
            raise
        return self._deliveries_from_xread(rows)

    def ack(self, entry_id: str) -> bool:
        acked = int(self._client.xack(self.name, self.group, entry_id) or 0)
        if acked:
            self._client.xdel(self.name, entry_id)
            self._client.hdel(self.error_hash, entry_id)
            self._client.zrem(self.deferred_zset, entry_id)
        return bool(acked)

    def fail(self, entry_id: str, error: str) -> None:
        self._client.hset(self.error_hash, entry_id, _redact_error(error))

    def defer(self, entry_id: str, *, until_ms: int) -> bool:
        """Record a server-visible due time without touching delivery metadata."""
        pending = self._pending_entry(entry_id)
        if pending is None:
            return False
        self._client.zadd(self.deferred_zset, {entry_id: int(until_ms)})
        return True

    # -- janitor: due-aware XCLAIM + DLQ -------------------------------

    def reclaim(self, consumer: str, count: int = 10) -> List[Delivery]:
        now_ms = self._server_time_ms()
        claim_ids: list[str] = []
        page_start = "-"
        page_size = 1000
        while len(claim_ids) < count:
            pending_rows = self._client.xpending_range(
                self.name, self.group, page_start, "+", page_size
            )
            if not pending_rows:
                break
            for row in pending_rows:
                pending = self._pending_from_row(row)
                if self._pending_idle_ms(row) < self.config.visibility_timeout_ms:
                    continue
                deferred_until = self._client.zscore(self.deferred_zset, pending.id)
                if deferred_until is not None and float(deferred_until) > now_ms:
                    continue
                claim_ids.append(pending.id)
                if len(claim_ids) >= count:
                    break
            if len(pending_rows) < page_size or len(claim_ids) >= count:
                break
            last_pending = self._pending_from_row(pending_rows[-1])
            page_start = f"({last_pending.id}"
        if not claim_ids:
            return []
        claimed = self._client.xclaim(
            self.name,
            self.group,
            consumer,
            self.config.visibility_timeout_ms,
            claim_ids,
        )
        out: list[Delivery] = []
        for entry_id, raw_fields in claimed:
            entry_id = _to_str(entry_id)
            pending = self._pending_entry(entry_id)
            delivery_count = pending.delivery_count if pending else 1
            if delivery_count > self.config.max_deliveries:
                self._dead_letter(entry_id, raw_fields, delivery_count)
                continue
            self._client.zrem(self.deferred_zset, entry_id)
            out.append(
                Delivery(
                    id=entry_id,
                    fields=_decode_fields(raw_fields),
                    delivery_count=delivery_count,
                )
            )
        return out

    def _server_time_ms(self) -> int:
        seconds, micros = self._client.time()
        return int(seconds) * 1000 + int(micros) // 1000

    @staticmethod
    def _pending_idle_ms(row: Any) -> int:
        if isinstance(row, dict):
            return int(_field(row, "time_since_delivered") or 0)
        return int(row[2])

    def _dead_letter(
        self, entry_id: str, raw_fields: Dict[str, Any], delivery_count: int
    ) -> None:
        now_ms = _wall_clock_ms()
        last_error = self._client.hget(self.error_hash, entry_id)
        dead_fields = {
            "source_id": entry_id,
            "fields_json": json.dumps(_decode_fields(raw_fields), sort_keys=True),
            "delivery_count": str(delivery_count),
            "enqueued_ms": str(_entry_ms(entry_id)),
            "dead_lettered_ms": str(now_ms),
            "last_error": _to_str(last_error) if last_error else "",
        }
        self._client.xadd(self.dead_stream, dead_fields)
        self.ack(entry_id)

    # -- introspection -------------------------------------------------

    def pending(self) -> List[PendingEntry]:
        rows = self._client.xpending_range(self.name, self.group, "-", "+", 1000)
        return [self._pending_from_row(row) for row in rows]

    def length(self) -> int:
        return int(self._client.xlen(self.name) or 0)

    def undelivered(self) -> int:
        return max(0, self.length() - len(self.pending()))

    def dead_letters(self) -> List[DeadLetter]:
        rows = self._client.xrange(self.dead_stream, min="-", max="+")
        return [self._dead_from_row(entry_id, fields) for entry_id, fields in rows]

    def backpressure(self) -> Dict[str, Any]:
        pending = len(self.pending())
        undelivered = self.undelivered()
        backlog = pending + undelivered
        return {
            "backlog": backlog,
            "high_water": self.config.high_water,
            "should_shed": backlog >= self.config.high_water,
            "undelivered": undelivered,
            "pending": pending,
            "dead": len(self.dead_letters()),
        }

    # -- operator: replay ----------------------------------------------

    def replay(self, dead_id: str) -> Optional[str]:
        rows = self._client.xrange(self.dead_stream, min=dead_id, max=dead_id)
        if not rows:
            return None
        _, fields = rows[0]
        payload_json = _field(fields, "fields_json") or "{}"
        new_id = self.add(json.loads(payload_json))
        self._client.xdel(self.dead_stream, dead_id)
        return new_id

    # -- Redis command helpers -----------------------------------------

    def _ensure_group(self) -> None:
        try:
            self._client.xgroup_create(self.name, self.group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP is expected
            if "BUSYGROUP" not in str(exc):
                raise

    def _xautoclaim(self, *, consumer: str, count: int) -> list[tuple[str, dict]]:
        raw = self._client.xautoclaim(
            self.name,
            self.group,
            consumer,
            self.config.visibility_timeout_ms,
            "0-0",
            count=count,
        )
        # redis-py returns (next_start_id, [(id, fields), ...], deleted_ids)
        # in newer versions and (next_start_id, [(id, fields), ...]) in older
        # ones. Fakes in tests may return either shape.
        entries = raw[1] if isinstance(raw, (tuple, list)) and len(raw) >= 2 else []
        return [(_to_str(entry_id), dict(fields)) for entry_id, fields in entries]

    def _deliveries_from_xread(self, rows: Any) -> list[Delivery]:
        out: list[Delivery] = []
        for stream_name, entries in rows or []:
            if _to_str(stream_name) != self.name:
                continue
            for entry_id, fields in entries:
                eid = _to_str(entry_id)
                pending = self._pending_entry(eid)
                out.append(
                    Delivery(
                        id=eid,
                        fields=_decode_fields(fields),
                        delivery_count=pending.delivery_count if pending else 1,
                    )
                )
        return out

    def _pending_entry(self, entry_id: str) -> Optional[PendingEntry]:
        rows = self._client.xpending_range(self.name, self.group, entry_id, entry_id, 1)
        if not rows:
            return None
        return self._pending_from_row(rows[0])

    def _pending_from_row(self, row: Any) -> PendingEntry:
        if isinstance(row, dict):
            entry_id = _field(row, "message_id") or _field(row, "id")
            consumer = _field(row, "consumer") or ""
            idle = int(_field(row, "time_since_delivered") or 0)
            deliveries = int(_field(row, "times_delivered") or 1)
        else:
            entry_id, consumer, idle, deliveries = row[:4]
        eid = _to_str(entry_id)
        now = _wall_clock_ms()
        return PendingEntry(
            id=eid,
            consumer=_to_str(consumer),
            delivery_count=int(deliveries),
            last_delivered_ms=now - int(idle),
            enqueued_ms=_entry_ms(eid),
            last_error=_to_str(self._client.hget(self.error_hash, eid) or "") or None,
        )

    def _dead_from_row(self, entry_id: Any, fields: Dict[str, Any]) -> DeadLetter:
        payload_json = _field(fields, "fields_json") or "{}"
        return DeadLetter(
            dead_id=_to_str(entry_id),
            source_id=_field(fields, "source_id") or "",
            fields=json.loads(payload_json),
            delivery_count=int(_field(fields, "delivery_count") or 0),
            enqueued_ms=int(_field(fields, "enqueued_ms") or 0),
            dead_lettered_ms=int(_field(fields, "dead_lettered_ms") or 0),
            last_error=_field(fields, "last_error") or None,
        )


def _encode_fields(fields: Dict[str, Any]) -> dict[str, str]:
    return {
        str(k): v if isinstance(v, str) else json.dumps(v, sort_keys=True)
        for k, v in fields.items()
    }


def _decode_fields(fields: Dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        text = _to_str(value)
        try:
            out[_to_str(key)] = json.loads(text)
        except json.JSONDecodeError:
            out[_to_str(key)] = text
    return out


def _field(fields: Dict[Any, Any], name: str) -> Optional[str]:
    if name in fields:
        return _to_str(fields[name])
    raw = name.encode()
    if raw in fields:
        return _to_str(fields[raw])
    return None


def _to_str(value: Any) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


def _wall_clock_ms() -> int:
    return int(_time.time() * 1000)


def _entry_ms(entry_id: str) -> int:
    ms, _, _ = entry_id.partition("-")
    try:
        return int(ms)
    except ValueError:
        return 0


def _redact_error(error: str, *, max_chars: int = 500) -> str:
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", str(error))
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED_SECRET]", text)
    return text[:max_chars]
