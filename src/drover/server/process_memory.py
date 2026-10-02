"""Process RSS guard; the hub budget excludes disposable query children."""

from __future__ import annotations

import atexit
import logging
import os
import subprocess
import threading
import time

from drover.config import MemoryBudgetConfig

log = logging.getLogger("drover.memory")


def process_rss(pid: int) -> int:
    if os.path.exists(f"/proc/{pid}/statm"):
        with open(f"/proc/{pid}/statm") as stream:
            return int(stream.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        capture_output=True,
        check=True,
        timeout=0.25,
    )
    return int(result.stdout.strip()) * 1024


class ProcessMemoryGuard:
    def __init__(
        self, config: MemoryBudgetConfig = MemoryBudgetConfig(), *, reader=process_rss
    ):
        self.config = config
        self.reader = reader
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._rss = None
        self._peak = 0
        self._state = "warn"
        self._error = False
        self._sampled_at = None
        self._last_warning = 0.0
        self._children = set()
        self._child_total = 0
        self._child_completed = 0
        self._child_peak = 0

    def sample(self):
        try:
            rss = self.reader(os.getpid())
            state = (
                "over"
                if rss > self.config.rss_budget_bytes
                else (
                    "warn"
                    if rss >= self.config.rss_budget_bytes * self.config.warn_fraction
                    else "ok"
                )
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            rss, state = None, "warn"
        now = time.monotonic()
        with self._lock:
            warn = state == "over" and (
                self._state != "over" or now - self._last_warning >= 60
            )
            self._rss, self._state, self._error, self._sampled_at = (
                rss,
                state,
                rss is None,
                now,
            )
            self._peak = max(self._peak, rss or 0)
            if warn:
                self._last_warning = now
        if warn:
            log.warning(
                "server RSS over budget: rss_bytes=%d budget_bytes=%d",
                rss,
                self.config.rss_budget_bytes,
            )

    def start(self):
        if self._thread is not None:
            return
        self.sample()

        def run():
            while not self._stop.wait(self.config.sample_interval_seconds):
                self.sample()

        self._thread = threading.Thread(
            target=run, name="hub-memory-guard", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def child_started(self, pid):
        with self._lock:
            self._children.add(pid)
            self._child_total += 1

    def child_sample(self, rss):
        with self._lock:
            self._child_peak = max(self._child_peak, rss)

    def child_finished(self, pid):
        with self._lock:
            if pid in self._children:
                self._children.remove(pid)
                self._child_completed += 1

    def snapshot(self):
        with self._lock:
            stale = (
                self._sampled_at is None
                or time.monotonic() - self._sampled_at
                > self.config.sample_interval_seconds * 2
            )
        if stale:
            self.sample()
        with self._lock:
            return {
                "rss_bytes": self._rss,
                "budget_bytes": self.config.rss_budget_bytes,
                "state": self._state,
                "peak_rss_bytes": self._peak,
                "measurement_error": self._error,
                "sample_interval_seconds": self.config.sample_interval_seconds,
                "query_children": {
                    "active": len(self._children),
                    "total": self._child_total,
                    "completed": self._child_completed,
                    "peak_rss_bytes": self._child_peak,
                    "rss_ceiling_bytes": 2 * 1024**3,
                },
            }


_guard = ProcessMemoryGuard()


def memory_guard():
    return _guard


def configure_memory_guard(config: MemoryBudgetConfig):
    global _guard
    _guard.stop()
    _guard = ProcessMemoryGuard(config)
    _guard.start()
    return _guard


atexit.register(lambda: _guard.stop())
