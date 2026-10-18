"""Low-overhead attribution for work performed inside the hub process.

The hub's physical footprint is a process-wide number.  This module makes a
later threshold crossing useful by retaining the request/background operation
that was active and by dumping every Python stack only at that crossing.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from drover.server.process_memory import hub_memory_bytes

log = logging.getLogger("drover.runtime_observability")

_activities_lock = threading.Lock()
_activities: dict[object, "_Activity"] = {}
_local = threading.local()


@dataclass(frozen=True)
class _Activity:
    kind: str
    name: str
    started_at: float
    thread_name: str


def _footprint(reader: Callable[[], int]) -> int | None:
    try:
        return int(reader())
    except (OSError, ValueError):
        return None


def _default_footprint_reader() -> int:
    return hub_memory_bytes(os.getpid())


def active_activities() -> list[dict[str, str]]:
    """A small, safe snapshot suitable for a single diagnostic log line."""
    with _activities_lock:
        activities = tuple(_activities.values())
    return [
        {"kind": item.kind, "name": item.name}
        for item in sorted(activities, key=lambda item: item.started_at)
    ]


def current_activity_holder() -> str:
    """Name the current operation without leaking request query parameters."""
    stack = getattr(_local, "activities", ())
    if stack:
        activity_item = stack[-1]
        return f"{activity_item.kind}:{activity_item.name}"
    return f"thread:{threading.current_thread().name}"


@contextmanager
def activity(
    name: str,
    *,
    kind: str,
    footprint_reader: Callable[[], int] | None = None,
) -> Iterator[None]:
    """Record one request or one bounded background pass.

    The two footprint reads are intentionally the only steady-state cost beyond
    one mutex-protected registry update and two structured log records.
    """
    reader = footprint_reader or _default_footprint_reader
    before = _footprint(reader)
    token = object()
    item = _Activity(
        kind=kind,
        name=name,
        started_at=time.monotonic(),
        thread_name=threading.current_thread().name,
    )
    with _activities_lock:
        _activities[token] = item
    stack = list(getattr(_local, "activities", ()))
    stack.append(item)
    _local.activities = stack
    log.info(
        "hub_activity start kind=%s name=%s footprint=%s",
        kind,
        name,
        before if before is not None else "unavailable",
    )
    try:
        yield
    finally:
        elapsed = time.monotonic() - item.started_at
        after = _footprint(reader)
        with _activities_lock:
            _activities.pop(token, None)
        stack.pop()
        _local.activities = stack
        delta = after - before if before is not None and after is not None else None
        log.info(
            "hub_activity end kind=%s name=%s footprint=%s footprint_delta=%s duration_s=%.3f",
            kind,
            name,
            after if after is not None else "unavailable",
            delta if delta is not None else "unavailable",
            elapsed,
        )


def dump_python_stacks() -> None:
    """Write a traceback for every live Python thread to the server log."""
    frames = sys._current_frames()
    for thread in threading.enumerate():
        frame = frames.get(thread.ident)
        if frame is None:
            continue
        log.error(
            "hub_footprint_watchdog stack thread=%s ident=%s\n%s",
            thread.name,
            thread.ident,
            "".join(traceback.format_stack(frame)),
        )


class FootprintWatchdog:
    """Dump attribution once on each crossing of the current-footprint limit."""

    def __init__(
        self,
        *,
        threshold_bytes: int = 1024**3,
        poll_seconds: float = 0.5,
        footprint_reader: Callable[[], int] | None = None,
        activities: Callable[[], list[dict[str, str]]] = active_activities,
        stack_dumper: Callable[[], None] = dump_python_stacks,
    ) -> None:
        self.threshold_bytes = threshold_bytes
        self.poll_seconds = poll_seconds
        self._reader = footprint_reader or _default_footprint_reader
        self._activities = activities
        self._stack_dumper = stack_dumper
        self._above = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample_once(self) -> None:
        current = _footprint(self._reader)
        if current is None:
            return
        above = current >= self.threshold_bytes
        crossing = above and not self._above
        self._above = above
        if not crossing:
            return
        log.error(
            "hub_footprint_watchdog crossing footprint=%d threshold=%d active=%s",
            current,
            self.threshold_bytes,
            self._activities(),
        )
        self._stack_dumper()

    def start(self) -> None:
        if self._thread is not None:
            return

        def run() -> None:
            while not self._stop.wait(self.poll_seconds):
                self.sample_once()

        self.sample_once()
        self._thread = threading.Thread(
            target=run, name="hub-footprint-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1)


@dataclass
class _LockHold:
    holder: str
    path: str
    acquired_at: float
    warned: bool = False


_lock_holds_guard = threading.Lock()
_lock_holds: dict[object, _LockHold] = {}
_lock_hold_watcher: threading.Thread | None = None
_lock_hold_wakeup = threading.Event()


def track_analytical_lock_acquire(*, holder: str, path: str) -> object:
    """Register an acquired lock for the one shared 30-second hold watchdog."""
    global _lock_hold_watcher
    token = object()
    with _lock_holds_guard:
        _lock_holds[token] = _LockHold(
            holder=holder, path=path, acquired_at=time.monotonic()
        )
        if _lock_hold_watcher is None or not _lock_hold_watcher.is_alive():
            _lock_hold_watcher = threading.Thread(
                target=_watch_lock_holds,
                name="analytical-lock-hold-watchdog",
                daemon=True,
            )
            _lock_hold_watcher.start()
    _lock_hold_wakeup.set()
    return token


def track_analytical_lock_release(token: object) -> float | None:
    with _lock_holds_guard:
        hold = _lock_holds.pop(token, None)
    _lock_hold_wakeup.set()
    return None if hold is None else time.monotonic() - hold.acquired_at


def _watch_lock_holds() -> None:
    while True:
        _lock_hold_wakeup.wait(1.0)
        _lock_hold_wakeup.clear()
        now = time.monotonic()
        with _lock_holds_guard:
            if not _lock_holds:
                return
            overdue = [
                item
                for item in _lock_holds.values()
                if not item.warned and now - item.acquired_at >= 30.0
            ]
            for item in overdue:
                item.warned = True
        for item in overdue:
            log.warning(
                "analytical_connect_lock held_over_30s holder=%s path=%s hold_s=%.3f",
                item.holder,
                item.path,
                now - item.acquired_at,
            )
