"""Optional Redis Streams *shadow* mirror for ingested AgentEvents.

Shadow mode publishes a copy of every newly-ingested event to a Redis Stream
so downstream consumers can start building against a streaming bus **without**
Redis becoming the source of truth. The lakehouse (parquet + DuckDB) remains
authoritative; the stream is a best-effort mirror.

Design guarantees:

* **Off by default / opt-in.** Disabled unless ``[redis_shadow] enabled = true``
  in the config, so a fresh install and local dev never touch Redis.
* **Never the source of truth.** Publishing is best-effort: any Redis error is
  logged and swallowed so a flaky/absent Redis can never block or fail ingest.
* **Stable idempotency keys.** Each stream entry carries ``idempotency_key`` set
  to the canonical :data:`drover.event_identity.CANONICAL_EVENT_IDENTITY`
  (``dedup_key``). The ingest path only hands us rows it has decided are new, so
  in normal operation the stream sees each logical event once; consumers that
  need to be defensive can dedupe on ``idempotency_key``.

The ``redis`` package is an optional dependency. It is imported lazily so that
nothing here is required unless shadow mode is actually enabled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from drover.event_identity import CANONICAL_EVENT_IDENTITY

log = logging.getLogger("drover.redis_shadow")

# Stream fields are flat strings. We mirror the stable business identity plus a
# small set of routing/debug fields; the full payload stays in the lakehouse.
_MIRRORED_FIELDS = (
    "id",
    "session_id",
    "agent_id",
    "event_type",
    "task_id",
    "repo_owner",
    "repo_name",
    "branch",
    "date",
)


@dataclass(frozen=True)
class RedisShadowConfig:
    """Resolved shadow-mode settings."""

    enabled: bool
    url: str
    stream: str
    maxlen: int

    @classmethod
    def from_runtime(
        cls,
        *,
        enabled: bool,
        url: str,
        stream: str,
        maxlen: int,
    ) -> "RedisShadowConfig":
        return cls(
            enabled=bool(enabled),
            url=url or "",
            stream=stream or "nexus:events",
            maxlen=max(0, int(maxlen)),
        )


def event_fields(row: dict) -> dict[str, str]:
    """Build the flat string field map for a single stream entry.

    The canonical ``dedup_key`` is surfaced as ``idempotency_key`` (the stable
    cross-system event identity) in addition to its raw column name.
    """
    fields: dict[str, str] = {}
    dedup_key = row.get(CANONICAL_EVENT_IDENTITY)
    if dedup_key is not None:
        fields["idempotency_key"] = str(dedup_key)
        fields[CANONICAL_EVENT_IDENTITY] = str(dedup_key)
    ts = row.get("timestamp")
    if ts is not None:
        # datetime → ISO; anything else stringified as-is.
        fields["timestamp"] = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    for name in _MIRRORED_FIELDS:
        value = row.get(name)
        if value is not None:
            fields[name] = str(value)
    return fields


class ShadowPublisher:
    """Best-effort publisher of ingested rows to a Redis Stream.

    The Redis client is injected so tests (and the ``--fake`` smoke path) can use
    an in-memory stand-in. Any subset of the ``redis`` client surface that
    implements ``xadd`` works.
    """

    def __init__(self, client: Any, *, stream: str, maxlen: int = 0) -> None:
        self._client = client
        self._stream = stream
        self._maxlen = max(0, int(maxlen))

    @property
    def stream(self) -> str:
        return self._stream

    def publish_rows(self, rows: Iterable[dict]) -> int:
        """Publish ``rows`` to the stream. Returns the count successfully added.

        Best-effort: an exception from the Redis client is logged once and the
        rest of the batch is skipped — ingest must never fail because the shadow
        mirror is unavailable.
        """
        published = 0
        for row in rows:
            fields = event_fields(row)
            if not fields:
                continue
            try:
                if self._maxlen:
                    self._client.xadd(
                        self._stream, fields, maxlen=self._maxlen, approximate=True
                    )
                else:
                    self._client.xadd(self._stream, fields)
                published += 1
            except Exception:  # noqa: BLE001 — shadow mirror must not break ingest
                log.warning(
                    "redis shadow publish failed for stream=%s (mirror is "
                    "best-effort; lakehouse remains source of truth)",
                    self._stream,
                    exc_info=True,
                )
                break
        return published


def build_publisher(
    cfg: RedisShadowConfig, *, client: Any = None
) -> Optional[ShadowPublisher]:
    """Construct a :class:`ShadowPublisher`, or ``None`` when shadow mode is off.

    When ``client`` is provided it is used directly (tests / fakes). Otherwise we
    lazily import ``redis`` and connect to ``cfg.url``. A missing ``redis``
    package or a connection failure logs a warning and returns ``None`` so the
    caller simply runs without the mirror.
    """
    if not cfg.enabled:
        return None
    if client is None:
        try:
            import redis  # type: ignore[import-not-found]
        except ImportError:
            log.warning(
                "redis_shadow enabled but the 'redis' package is not installed; "
                "shadow mirror disabled. Install drover[redis] to enable it."
            )
            return None
        try:
            client = redis.Redis.from_url(cfg.url, decode_responses=True)
            client.ping()
        except Exception:  # noqa: BLE001
            log.warning(
                "redis_shadow enabled but connecting to %s failed; shadow "
                "mirror disabled (lakehouse ingest continues normally).",
                cfg.url,
                exc_info=True,
            )
            return None
    return ShadowPublisher(client, stream=cfg.stream, maxlen=cfg.maxlen)


def read_events(
    client: Any, stream: str, *, count: Optional[int] = None, start: str = "-"
) -> list[tuple[str, dict[str, str]]]:
    """Read entries from ``stream`` via ``XRANGE``. Returns ``[(id, fields)]``.

    Decodes bytes → str so callers get uniform output whether the client was
    created with ``decode_responses=True`` or not.
    """
    raw = client.xrange(stream, min=start, max="+", count=count)
    out: list[tuple[str, dict[str, str]]] = []
    for entry_id, fields in raw:
        out.append(
            (_to_str(entry_id), {_to_str(k): _to_str(v) for k, v in fields.items()})
        )
    return out


def _to_str(value: Any) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


class InMemoryStreamClient:
    """Tiny in-memory stand-in for the Redis Streams client surface we use.

    Implements just enough (``xadd`` / ``xrange`` / ``xlen``) for tests and the
    ``redis-shadow-smoke --fake`` command to prove publish/read behavior without
    a running Redis server. Entry IDs are monotonically increasing ``"<n>-0"``.
    """

    def __init__(self) -> None:
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._seq = 0

    def xadd(
        self,
        name: str,
        fields: dict[str, str],
        *,
        maxlen: Optional[int] = None,
        approximate: bool = True,
    ) -> str:
        self._seq += 1
        entry_id = f"{self._seq}-0"
        entries = self._streams.setdefault(name, [])
        entries.append((entry_id, dict(fields)))
        if maxlen and len(entries) > maxlen:
            del entries[: len(entries) - maxlen]
        return entry_id

    def xrange(
        self, name: str, min: str = "-", max: str = "+", count: Optional[int] = None
    ) -> list[tuple[str, dict[str, str]]]:
        entries = list(self._streams.get(name, []))
        return entries[:count] if count is not None else entries

    def xlen(self, name: str) -> int:
        return len(self._streams.get(name, []))

    def ping(self) -> bool:  # parity with redis.Redis for build_publisher
        return True
