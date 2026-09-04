"""Foreground-aware admission for bounded analytical maintenance work."""

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
