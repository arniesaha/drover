"""What ``/readyz`` has to prove before it answers 200.

Issue #175. During the 2026-08-14 outage the hub answered ``/readyz`` with
``200 ok`` for hours while every query raised ``FATAL Error: Failed: database
has been invalidated because of a previous fatal error``. Once a DuckDB
instance is invalidated every later query fails until the process restarts,
but the process stays alive and launchd reports status 0 -- so from outside, a
hub serving nothing looked exactly like a working one. ``/healthz`` answering
"the process is running" is correct and unchanged; readiness is the signal
that was lying.

The probe is deliberately small, and each choice is load-bearing:

* **Both stores.** The lakehouse and the control plane are separate DuckDB
  *instances* in separate files (see ``control_plane_path``), so either can be
  invalidated while the other is fine. A probe that checked one would still be
  lying about the other -- and ``/harness*``, the fleet's whole surface, is
  served from the control plane.

* **``SELECT 1``, on a handle the process already holds.** Invalidation is
  checked in DuckDB's ``ClientContext::BeginQueryInternal`` before a query does
  any work, so the cheapest possible statement detects it. The analytical
  probe borrows a live connection from ``live_connections`` and runs on a
  ``cursor()`` of it: a cursor is an independent connection on the *same*
  instance, so it sees the same invalidation, costs ~70us, takes no lock, and
  (measured) does not queue behind a long query already running on the handle
  it was borrowed from.

* **No connect of its own on the analytical store.** ``duckdb.connect`` on a
  saturated instance is where ``sample(1)`` found threads parked during #95,
  and a readiness endpoint that can be polled every second must not add to
  that. Opening the store would be worse than the lock: when the process holds
  nothing, a connect *builds* the instance -- catalog load, WAL replay -- and
  closing the last connection checkpoints on the way out. Every few seconds,
  against a 700MB store, that is a new background load, which is exactly the
  mistake #76, #93 and #91 were each about.

* **Nothing open is not a failure.** The instance is kept alive by its
  connections and DuckDB's instance cache holds only a weak reference, so a
  process holding no connection has no invalidated instance to find: the next
  connect builds a fresh one. Reporting an idle hub as unready would be the
  false alarm that gets readiness ignored.

* **...unless the last real open failed.** A store nothing can open leaves no
  handle to borrow, which from the inside looks exactly like an idle hub --
  the same blindness one layer down. So the openers report:
  ``open_duckdb_connection`` records the failures it raises, and a recent one
  is read here as the store's answer. It costs nothing to collect and it is a
  real worker's real error rather than a probe of our own.

The control-plane probe does use ``control_plane_connection``, which opens a
connection per window unless the hub pinned one. That is the same window every
``/harness*`` request takes, on a store of a few megabytes, so it is a real
measurement of the real path rather than an extra load source -- and a brief
cache keeps a hot poller from multiplying it.

**What this cannot see.** It proves the instance answers, not that every block
in the store is readable: measured against DuckDB 1.5.2, a corrupt data block
raises ``IOException`` on the scan that reads it *without* invalidating the
database, and ``SELECT 1`` keeps working. Only FATAL/INTERNAL errors invalidate
(``Exception::InvalidatesDatabase``), and that is the failure mode #175 is
about. Readiness is also not restart logic: this module only surfaces the
state.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from drover.server.db import (
    control_plane_connection,
    control_plane_path,
    last_connect_failure,
    live_connections,
)

log = logging.getLogger("drover.readiness")

#: The two stores a hub serves from, named in the response body so an operator
#: reading a 503 knows which one to look at.
STORE_ANALYTICAL = "analytical"
STORE_CONTROL_PLANE = "control_plane"

#: Per-store outcomes. Only ``failed`` makes the hub unready; the rest are
#: reported so a green ``/readyz`` still says what it actually proved.
STATE_OK = "ok"
STATE_IDLE = "idle"
STATE_ABSENT = "absent"
STATE_BUSY = "busy"
STATE_FAILED = "failed"

#: The whole probe. Anything heavier would be a query, and a readiness
#: endpoint that runs queries is a readiness endpoint that causes outages.
PROBE_SQL = "SELECT 1"

#: How long a good verdict is reused. Long enough that a poller cannot
#: multiply into load, short enough that a handle invalidated now surfaces on
#: the next poll rather than the one after. Failures are never cached.
DEFAULT_CACHE_SECONDS = 2.0

#: DuckDB grants one process at a time write access to a file, and the
#: single-machine setup in getting-started.md has harnessd sharing the hub's
#: store, so "another process is holding it" is a routine collision rather
#: than an outage -- ``bootstrap_harnessd_schema`` already treats it as one.
#: It only becomes an outage when it never clears, so it is reported as
#: ``busy`` until it has persisted this long.
DEFAULT_BUSY_GRACE_SECONDS = 30.0

#: How recently a real open must have failed for readiness to still hold it
#: against the store. Long enough to outlast the gap between the workers that
#: touch the lakehouse, short enough that a failure from an hour ago -- a
#: transient the hub has since recovered from -- is not still being reported.
DEFAULT_CONNECT_FAILURE_WINDOW_SECONDS = 120.0

#: DuckDB's wording when another process owns the file lock.
_LOCK_CONFLICT_MARKERS = ("could not set lock", "conflicting lock")

#: Details are for a human reading a 503, and DuckDB's fatal messages carry a
#: paragraph of advice. Enough to identify the failure, not enough to make the
#: body a log file.
_MAX_DETAIL_CHARS = 400


def _cache_seconds_default() -> float:
    raw = os.environ.get("DROVER_READYZ_CACHE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_CACHE_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("ignoring unparseable DROVER_READYZ_CACHE_SECONDS=%r", raw)
        return DEFAULT_CACHE_SECONDS


def _detail(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip().replace("\n", " ")
    if len(text) > _MAX_DETAIL_CHARS:
        text = text[: _MAX_DETAIL_CHARS - 3] + "..."
    return text


def _text_is_lock_conflict(message: str) -> bool:
    text = message.lower()
    return any(marker in text for marker in _LOCK_CONFLICT_MARKERS)


def _is_lock_conflict(exc: BaseException) -> bool:
    return _text_is_lock_conflict(str(exc))


@dataclass(frozen=True)
class StoreProbe:
    """One store's answer to "can this instance serve?"."""

    store: str
    state: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state != STATE_FAILED

    def as_dict(self, *, include_detail: bool = True) -> dict[str, object]:
        return {
            "store": self.store,
            "state": self.state,
            "detail": self.detail if include_detail else "",
        }


@dataclass(frozen=True)
class ReadinessReport:
    """Every store's verdict, and the one the HTTP status is taken from."""

    stores: tuple[StoreProbe, ...]
    checked_at: float

    @property
    def ok(self) -> bool:
        return all(store.ok for store in self.stores)

    def failures(self) -> tuple[StoreProbe, ...]:
        return tuple(store for store in self.stores if not store.ok)

    def as_dict(self, *, include_detail: bool = True) -> dict[str, object]:
        return {
            "ready": self.ok,
            "checked_at": datetime.fromtimestamp(
                self.checked_at, tz=timezone.utc
            ).isoformat(),
            "stores": [
                store.as_dict(include_detail=include_detail) for store in self.stores
            ],
        }

    def as_response(self, *, include_detail: bool = True) -> tuple[int, str]:
        """The HTTP status and body. 503 names the store, not just the state.

        ``/readyz`` is one of the few unauthenticated routes -- a monitor
        cannot hold a credential -- so the verdict and the store names are
        public but ``detail`` is not: DuckDB's errors quote the store's
        filesystem path, and an anonymous caller has no business learning it.
        """
        return (200 if self.ok else 503), json.dumps(
            self.as_dict(include_detail=include_detail), sort_keys=True
        ) + "\n"


class ReadinessProbe:
    """Answers ``/readyz`` by asking both stores whether they still work."""

    def __init__(
        self,
        duckdb_path: str | Path,
        *,
        cache_seconds: float | None = None,
        busy_grace_seconds: float = DEFAULT_BUSY_GRACE_SECONDS,
        connect_failure_window: float = DEFAULT_CONNECT_FAILURE_WINDOW_SECONDS,
        time_source=time.monotonic,
    ) -> None:
        self._duckdb_path = Path(duckdb_path)
        self._cache_seconds = (
            _cache_seconds_default()
            if cache_seconds is None
            else max(0.0, cache_seconds)
        )
        self._busy_grace_seconds = busy_grace_seconds
        self._connect_failure_window = connect_failure_window
        self._time = time_source
        self._lock = threading.Lock()
        self._cached: ReadinessReport | None = None
        self._cached_until = 0.0
        self._busy_since: dict[str, float] = {}
        self._last_ok: bool | None = None

    def check(self) -> ReadinessReport:
        """Probe both stores, or reuse a verdict from the last couple of seconds.

        Serialized: N pollers arriving together cost one probe, not N. The
        probe itself is bounded -- a borrowed cursor never waits, and the
        control-plane window is the same one ``/harness`` takes -- so waiting
        here is waiting for microseconds to milliseconds.
        """
        with self._lock:
            now = self._time()
            cached = self._cached
            if cached is not None and cached.ok and now < self._cached_until:
                return cached
            report = ReadinessReport(
                stores=(self._probe_analytical(now), self._probe_control_plane(now)),
                checked_at=time.time(),
            )
            self._cached = report
            self._cached_until = now + self._cache_seconds
            self._log_transition(report)
            return report

    def _log_transition(self, report: ReadinessReport) -> None:
        """Say it in the log the first time it changes, and only then.

        The outage left no trace in readiness at all; a line per poll would
        leave too much.
        """
        if self._last_ok is report.ok:
            return
        self._last_ok = report.ok
        if report.ok:
            log.info("readiness recovered: every store answers")
        else:
            log.warning(
                "readiness failing: %s",
                "; ".join(
                    f"{store.store}: {store.detail}" for store in report.failures()
                ),
            )

    def _probe_analytical(self, now: float) -> StoreProbe:
        """Run ``SELECT 1`` on a handle this process is already holding.

        Every handle is tried, because a connection that has been closed but
        not yet collected proves nothing either way -- ``LedgerWriter`` and
        friends keep theirs on an attribute -- and calling that unready would
        be a false alarm on every shutdown. A handle that is *open* and cannot
        answer is the failure this endpoint exists for.
        """
        handles = live_connections(self._duckdb_path)
        if not handles:
            return self._idle_or_unopenable(
                "this process holds no connection, so no instance can be invalid",
                now,
            )
        stale = 0
        last_failure: BaseException | None = None
        for con in handles:
            try:
                cursor = con.cursor()
            except duckdb.ConnectionException:
                stale += 1
                continue
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                last_failure = exc
                continue
            try:
                cursor.execute(PROBE_SQL).fetchone()
            except duckdb.ConnectionException:
                stale += 1
                continue
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                return self._failure(STORE_ANALYTICAL, exc, now)
            finally:
                try:
                    cursor.close()
                except Exception:  # noqa: BLE001 - teardown is best effort
                    log.debug("failed to close the readiness cursor")
            self._busy_since.pop(STORE_ANALYTICAL, None)
            return StoreProbe(
                STORE_ANALYTICAL, STATE_OK, f"{PROBE_SQL} on a live handle"
            )
        if last_failure is not None:
            return self._failure(STORE_ANALYTICAL, last_failure, now)
        return self._idle_or_unopenable(
            f"{stale} closed handle(s) awaiting collection; no live instance", now
        )

    def _idle_or_unopenable(self, idle_detail: str, now: float) -> StoreProbe:
        """Idle, unless the last real attempt to open the store just failed.

        A borrowed handle cannot speak for a store that nothing can open --
        there is no handle to borrow -- and that state looks identical to an
        idle hub from the inside. So the openers report: ``open_duckdb_connection``
        records its failures, and a recent one is the difference between a hub
        with nothing to do and a hub with nothing to do it with.
        """
        failure = last_connect_failure(self._duckdb_path)
        if failure is None:
            return StoreProbe(STORE_ANALYTICAL, STATE_IDLE, idle_detail)
        age, message = failure
        if age > self._connect_failure_window:
            return StoreProbe(
                STORE_ANALYTICAL,
                STATE_IDLE,
                f"{idle_detail}; last open failed {age:.0f}s ago",
            )
        return self._classify(
            STORE_ANALYTICAL,
            f"opening the store failed {age:.0f}s ago: {message}",
            lock_conflict=_text_is_lock_conflict(message),
            now=now,
        )

    def _probe_control_plane(self, now: float) -> StoreProbe:
        """Run ``SELECT 1`` through the window ``/harness*`` uses.

        Never opens the analytical store and never takes its lock:
        ``control_plane_connection`` resolves to the control-plane file and
        holds the control-plane lock, which is the isolation
        ``tests/test_control_plane_isolation.py`` enforces.
        """
        path = control_plane_path(self._duckdb_path)
        if not path.exists():
            # Connecting would create it. A readiness poll must not bootstrap
            # a store as a side effect.
            return StoreProbe(
                STORE_CONTROL_PLANE, STATE_ABSENT, f"no control-plane store at {path}"
            )
        try:
            with control_plane_connection(self._duckdb_path) as con:
                con.execute(PROBE_SQL).fetchone()
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return self._failure(STORE_CONTROL_PLANE, exc, now)
        self._busy_since.pop(STORE_CONTROL_PLANE, None)
        return StoreProbe(STORE_CONTROL_PLANE, STATE_OK, f"{PROBE_SQL} on the store")

    def _failure(self, store: str, exc: BaseException, now: float) -> StoreProbe:
        """Classify a raised failure."""
        return self._classify(
            store, _detail(exc), lock_conflict=_is_lock_conflict(exc), now=now
        )

    def _classify(
        self, store: str, detail: str, *, lock_conflict: bool, now: float
    ) -> StoreProbe:
        """Report a failure, holding back only on a file-lock collision."""
        if not lock_conflict:
            self._busy_since.pop(store, None)
            return StoreProbe(store, STATE_FAILED, detail)
        since = self._busy_since.setdefault(store, now)
        held_for = now - since
        if held_for < self._busy_grace_seconds:
            return StoreProbe(
                store,
                STATE_BUSY,
                f"another process holds the store ({held_for:.0f}s): {detail}",
            )
        return StoreProbe(
            store,
            STATE_FAILED,
            f"another process has held the store for {held_for:.0f}s: {detail}",
        )
