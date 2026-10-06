"""Process memory guard; the hub budget excludes disposable query children.

The budget is checked against the memory the process actually owns. On Linux
that is RSS. On macOS it is ``phys_footprint`` (what Activity Monitor and
``footprint`` report): there RSS also counts pages the allocator has freed
but keeps resident ("Malloc Small (empty)"), so a hub that peaked once stays
"over" forever. Prod after the v2 switch: RSS 4.54 GB, footprint 266 MB.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import struct
import subprocess
import sys
import threading
import time
from functools import lru_cache

from drover.config import MemoryBudgetConfig

log = logging.getLogger("drover.memory")


@lru_cache(maxsize=1)
def _darwin_pidinfo():
    function = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidinfo
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    function.restype = ctypes.c_int
    return function


def process_rss(pid: int) -> int:
    if sys.platform == "darwin":
        # macOS SDK sys/proc_info.h: PROC_PIDTASKINFO=4; proc_taskinfo is
        # six uint64_t values followed by twelve int32_t values. RSS is bytes.
        buffer = ctypes.create_string_buffer(96)
        if _darwin_pidinfo()(pid, 4, 0, buffer, len(buffer)) != len(buffer):
            raise OSError(ctypes.get_errno(), "process RSS unavailable")
        return struct.unpack_from("=Q", buffer, 8)[0]
    if os.path.exists(f"/proc/{pid}/statm"):
        with open(f"/proc/{pid}/statm") as stream:
            return int(stream.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            check=True,
            timeout=0.25,
        )
    except subprocess.CalledProcessError as exc:
        raise OSError(exc.returncode, "process RSS unavailable") from exc
    return int(result.stdout.strip()) * 1024


@lru_cache(maxsize=1)
def _darwin_rusage():
    function = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pid_rusage
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    function.restype = ctypes.c_int
    return function


def process_footprint(pid: int) -> tuple[int, int]:
    """``(phys_footprint, lifetime_max_phys_footprint)`` in bytes (macOS).

    macOS SDK sys/resource.h: RUSAGE_INFO_V4=4; ``rusage_info_v4`` is a
    16-byte uuid then uint64_t fields, ``ri_phys_footprint`` at offset 72 and
    ``ri_lifetime_max_phys_footprint`` at 240 (296 bytes in all). The
    lifetime maximum is the kernel's own high-water mark, so a startup spike
    between two samples is still reported.
    """
    buffer = ctypes.create_string_buffer(296)
    if _darwin_rusage()(pid, 4, buffer) != 0:
        raise OSError(ctypes.get_errno(), "process footprint unavailable")
    return (
        struct.unpack_from("=Q", buffer, 72)[0],
        struct.unpack_from("=Q", buffer, 240)[0],
    )


#: What the hub budget is checked against on this platform.
HUB_MEASUREMENT = "phys_footprint" if sys.platform == "darwin" else "rss"


def hub_memory_bytes(pid: int) -> int:
    if HUB_MEASUREMENT == "phys_footprint":
        return process_footprint(pid)[0]
    return process_rss(pid)


_SAMPLE_ERRORS = (OSError, ValueError, subprocess.SubprocessError)


def memory_note(pid: int | None = None) -> str:
    """One log-friendly reading, e.g. after each startup phase.

    On macOS the lifetime peak is the kernel's high-water mark, so the phase
    that set it is the first one whose note shows it.
    """
    pid = os.getpid() if pid is None else pid
    parts = []
    try:
        if HUB_MEASUREMENT == "phys_footprint":
            footprint, lifetime = process_footprint(pid)
            parts += [f"phys_footprint={footprint}", f"lifetime_peak={lifetime}"]
        parts.append(f"rss={process_rss(pid)}")
    except _SAMPLE_ERRORS:
        parts.append("memory=unavailable")
    return " ".join(parts)


class ProcessMemoryGuard:
    def __init__(
        self,
        config: MemoryBudgetConfig = MemoryBudgetConfig(),
        *,
        reader=None,
        measurement: str | None = None,
    ):
        """``reader(pid)`` returns the budgeted bytes; it measures ``measurement``.

        By default that is this platform's :data:`HUB_MEASUREMENT`. A custom
        reader is taken to measure RSS unless told otherwise.
        """
        self.config = config
        self.reader = reader or hub_memory_bytes
        self.measurement = measurement or (HUB_MEASUREMENT if reader is None else "rss")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._measured = None
        self._peak = 0
        self._rss = None
        self._rss_peak = 0
        self._lifetime_peak = None
        self._state = "warn"
        self._error = False
        self._sampled_at = None
        self._last_warning = 0.0
        self._children = set()
        self._child_total = 0
        self._child_completed = 0
        self._child_peak = 0

    def _side_readings(self, pid: int, measured):
        """``(rss, lifetime peak footprint)``: reported, never budgeted."""
        if self.measurement == "rss":
            return measured, None
        try:
            rss = process_rss(pid)
        except _SAMPLE_ERRORS:
            rss = None
        lifetime = None
        if self.measurement == "phys_footprint":
            try:
                lifetime = process_footprint(pid)[1]
            except _SAMPLE_ERRORS:
                pass
        return rss, lifetime

    def sample(self):
        pid = os.getpid()
        try:
            measured = self.reader(pid)
            state = (
                "over"
                if measured > self.config.rss_budget_bytes
                else (
                    "warn"
                    if measured
                    >= self.config.rss_budget_bytes * self.config.warn_fraction
                    else "ok"
                )
            )
        except _SAMPLE_ERRORS:
            measured, state = None, "warn"
        rss, lifetime = self._side_readings(pid, measured)
        now = time.monotonic()
        with self._lock:
            warn = state == "over" and (
                self._state != "over" or now - self._last_warning >= 60
            )
            self._measured, self._state, self._error, self._sampled_at = (
                measured,
                state,
                measured is None,
                now,
            )
            self._peak = max(self._peak, measured or 0)
            self._rss = rss
            self._rss_peak = max(self._rss_peak, rss or 0)
            if lifetime is not None:
                self._lifetime_peak = lifetime
            if warn:
                self._last_warning = now
        if warn:
            label = "RSS" if self.measurement == "rss" else self.measurement
            log.warning(
                "server %s over budget: %s_bytes=%d budget_bytes=%d",
                label,
                self.measurement,
                measured,
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
        if self._thread is not None and self._thread.is_alive():
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
            extra = (
                {"lifetime_peak_footprint_bytes": self._lifetime_peak}
                if self.measurement == "phys_footprint"
                else {}
            )
            return {
                # ``state`` compares ``measured_bytes`` with ``budget_bytes``;
                # RSS is reported alongside it, on macOS for diagnosis only.
                "measurement": self.measurement,
                "measured_bytes": self._measured,
                "peak_measured_bytes": self._peak,
                **extra,
                "rss_bytes": self._rss,
                "budget_bytes": self.config.rss_budget_bytes,
                "state": self._state,
                "peak_rss_bytes": self._rss_peak,
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
