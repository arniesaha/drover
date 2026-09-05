"""Foreground-aware admission for bounded analytical maintenance work.

The analytical store is one DuckDB instance whose ``threads`` and
``memory_limit`` are instance-wide: a background pass does not get its own
budget, it changes everyone's. On 2026-09-04 concurrent analytical work left
the server at 374 percent CPU and every ``/harness*`` endpoint timed out
while ``/healthz`` answered in 3 ms -- the control plane has its own instance
and its own lock, so it was not blocked, it was starved of CPU (#331).

The gate is the cheap half of the answer: background passes stand aside while
a request is being served. Standing aside forever would be its own bug, so
admission is bounded -- see ``MaintenanceAdmission``.

The gate class here is taken from the closed PR #324, whose projection did
not pay for itself but whose admission control was sound and independently
reviewed as deadlock-free.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

_latest_gate_lock = threading.Lock()
_latest_gate: "AnalyticalMaintenanceGate | None" = None


@dataclass(frozen=True)
class MaintenanceGateStats:
    foreground_waiters: int
    maintenance_active: bool


def latest_maintenance_gate_stats() -> MaintenanceGateStats:
    """Return the active process gate snapshot without any storage access."""
    with _latest_gate_lock:
        gate = _latest_gate
    return gate.stats() if gate is not None else MaintenanceGateStats(0, False)


class AnalyticalMaintenanceGate:
    """Allow one background analytical pass only when no request is waiting.

    This coordinates threads in one server process. It deliberately does not
    own a DuckDB connection, a transaction, or cross-process state.
    """

    def __init__(self) -> None:
        global _latest_gate
        self._lock = threading.Lock()
        self._foreground_waiters = 0
        self._maintenance_active = False
        with _latest_gate_lock:
            _latest_gate = self

    @contextmanager
    def foreground(self) -> Iterator[None]:
        """Register a foreground request before it opens the analytical store."""
        with self._lock:
            self._foreground_waiters += 1
        try:
            yield
        finally:
            with self._lock:
                self._foreground_waiters -= 1

    def try_begin_maintenance(self) -> bool:
        """Acquire the one maintenance slot without delaying a foreground request."""
        with self._lock:
            if self._foreground_waiters or self._maintenance_active:
                return False
            self._maintenance_active = True
            return True

    def end_maintenance(self) -> None:
        """Release a slot obtained by try_begin_maintenance()."""
        with self._lock:
            if not self._maintenance_active:
                raise RuntimeError("analytics maintenance was not active")
            self._maintenance_active = False

    def stats(self) -> MaintenanceGateStats:
        with self._lock:
            return MaintenanceGateStats(
                foreground_waiters=self._foreground_waiters,
                maintenance_active=self._maintenance_active,
            )


class MaintenanceAdmission:
    """One worker's view of the gate, with a floor under how long it defers.

    A gate on its own trades an outage for silent staleness: this hub is
    polled by a phone every few seconds, so "run only when nothing is in
    flight" can mean "never". After ``max_consecutive_skips`` refusals the
    worker runs anyway, which bounds how stale its table can get at roughly
    ``max_consecutive_skips * poll_interval``. Yielding is a courtesy, not a
    promise.
    """

    def __init__(
        self,
        gate: "AnalyticalMaintenanceGate | None",
        *,
        max_consecutive_skips: int = 10,
    ) -> None:
        if max_consecutive_skips < 0:
            raise ValueError("max_consecutive_skips must not be negative")
        self._gate = gate
        self._max_consecutive_skips = max_consecutive_skips
        self._lock = threading.Lock()
        self._consecutive_skips = 0
        self._skipped_total = 0
        self._forced_total = 0

    @property
    def skipped_total(self) -> int:
        with self._lock:
            return self._skipped_total

    @property
    def forced_total(self) -> int:
        with self._lock:
            return self._forced_total

    @contextmanager
    def admit(self) -> Iterator[bool]:
        """Yield whether this pass should run, releasing the slot afterwards."""
        if self._gate is None:
            yield True
            return
        if self._gate.try_begin_maintenance():
            with self._lock:
                self._consecutive_skips = 0
            try:
                yield True
            finally:
                self._gate.end_maintenance()
            return
        with self._lock:
            self._consecutive_skips += 1
            self._skipped_total += 1
            forced = self._consecutive_skips > self._max_consecutive_skips
            if forced:
                self._consecutive_skips = 0
                self._forced_total += 1
        # Deliberately unslotted: the gate refused, so this pass runs beside
        # whatever holds it rather than pretending to own the slot.
        yield bool(forced)
