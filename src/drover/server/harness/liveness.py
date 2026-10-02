"""One definition of host liveness: online / stale / offline / retired.

Every reader -- the fleet snapshot, provider capacity refresh, launch pickers,
content-consent fan-out, MCP and the web/iOS clients -- gets its answer from
:func:`host_liveness`, which derives it from heartbeat age. The stored
``status`` column is only what the host last claimed about itself; a host that
went dark days ago still has ``online`` stored, so nothing may read it directly.

Thresholds (seconds of heartbeat silence), overridable per hub through the
environment:

* ``DROVER_HOST_STALE_AFTER_SECONDS`` (default 45) -- missed a few 15 s beats;
  still shown and launchable, but flagged.
* ``DROVER_HOST_OFFLINE_AFTER_SECONDS`` (default 600) -- two provider refresh
  intervals; the host is treated as gone (never launched on, quota marked
  host-offline).

Connection kind does not matter for the age test: relay hosts register through
the same HTTP heartbeat as direct ones. The relay socket only adds a veto -- a
spoke the hub knows to be unresponsive is offline whatever its last heartbeat.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

Liveness = Literal["online", "stale", "offline", "retired"]

DEFAULT_STALE_AFTER_SECONDS = 45.0
DEFAULT_OFFLINE_AFTER_SECONDS = 600.0

STALE_ENV = "DROVER_HOST_STALE_AFTER_SECONDS"
OFFLINE_ENV = "DROVER_HOST_OFFLINE_AFTER_SECONDS"


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def liveness_thresholds() -> tuple[float, float]:
    """``(stale_after, offline_after)`` seconds, offline never below stale."""
    stale = _env_seconds(STALE_ENV, DEFAULT_STALE_AFTER_SECONDS)
    offline = _env_seconds(OFFLINE_ENV, DEFAULT_OFFLINE_AFTER_SECONDS)
    return stale, max(offline, stale)


@dataclass(frozen=True)
class HostLiveness:
    state: Liveness
    heartbeat_age_seconds: float | None
    stale_after_seconds: float
    offline_after_seconds: float

    @property
    def online(self) -> bool:
        return self.state == "online"

    @property
    def usable(self) -> bool:
        """Worth dialling or launching on: online, or stale but not yet gone."""
        return self.state in ("online", "stale")


def heartbeat_age_seconds(
    last_seen_at: datetime | None, now: datetime | None = None
) -> float | None:
    """Seconds since ``last_seen_at``; None for a host that never heartbeat.

    A naive ``last_seen_at`` straight off a DuckDB ``TIMESTAMP`` is process-
    *local* wall time, not UTC (see ``registry._db_timestamp_to_utc``), so an
    aware ``now`` is converted to local before comparing; converting to naive
    UTC would shift the age by the process's UTC offset.
    """
    if last_seen_at is None:
        return None
    if now is None:
        now = (
            datetime.now(timezone.utc)
            if last_seen_at.tzinfo is not None
            else datetime.now()
        )
    elif last_seen_at.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    elif last_seen_at.tzinfo is None and now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    return max((now - last_seen_at).total_seconds(), 0.0)


def host_liveness(
    host: Any,
    *,
    now: datetime | None = None,
    relay_responsive: bool | None = None,
    stale_after: float | None = None,
    offline_after: float | None = None,
) -> HostLiveness:
    """Derive a host's liveness from its heartbeat age.

    ``relay_responsive`` is the relay manager's verdict for a relay host
    (``None`` when unknown or not a relay host). ``False`` vetoes to offline.
    A host that never heartbeat is judged by its stored status alone.
    """
    default_stale, default_offline = liveness_thresholds()
    stale_s = default_stale if stale_after is None else stale_after
    offline_s = max(default_offline if offline_after is None else offline_after, stale_s)
    age = heartbeat_age_seconds(getattr(host, "last_seen_at", None), now)

    def result(state: Liveness) -> HostLiveness:
        return HostLiveness(state, age, stale_s, offline_s)

    if getattr(host, "retired_at", None) is not None:
        return result("retired")
    stored = str(getattr(host, "status", "online") or "").lower()
    if stored == "retired":
        return result("retired")
    if stored != "online":
        return result("offline")
    if getattr(host, "connection_kind", "direct") == "relay" and relay_responsive is False:
        return result("offline")
    if age is None:
        return result("online")
    if age > offline_s:
        return result("offline")
    if age > stale_s:
        return result("stale")
    return result("online")
