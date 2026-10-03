"""Opt-in, bounded stack sampling before server-wide imports.

Only standard-library imports belong here. This observes a planned startup;
it never exits, retries, restarts or changes a service deadline.
"""

from __future__ import annotations

import atexit
import faulthandler
import os
import sys
import threading
import time

STACK_INTERVAL_ENV = "DROVER_STARTUP_STACK_INTERVAL_SECONDS"
DIAGNOSTIC_WINDOW_SECONDS = 75.0


def _marker(stream, phase: str) -> None:
    print(
        f"startup diagnostic phase={phase} pid={os.getpid()} "
        f"runtime={sys.executable!r} monotonic={time.monotonic():.3f}",
        file=stream,
        flush=True,
    )


class StartupDiagnostics:
    def __init__(self, stream) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._closed = False
        self._timer = threading.Timer(DIAGNOSTIC_WINDOW_SECONDS, self.close)
        self._timer.daemon = True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._timer.cancel()
            faulthandler.cancel_dump_traceback_later()
            try:
                _marker(self._stream, "diagnostic_window_closed")
            except (OSError, ValueError):
                pass


def arm_startup_diagnostics() -> StartupDiagnostics | None:
    """Sample stacks every 1–30 seconds for at most 75 seconds, if requested.

    The default performs no I/O and starts no thread. Invalid settings are
    ignored without logging their value. Use 15 for a 90-second startup gate.
    """
    raw = os.environ.get(STACK_INTERVAL_ENV, "").strip()
    if raw in {"", "0"}:
        return None
    stream = sys.__stderr__
    if stream is None:
        return None
    try:
        interval = int(raw)
        if not 1 <= interval <= 30:
            raise ValueError("out of range")
    except ValueError:
        try:
            _marker(stream, "invalid_stack_interval_ignored")
        except (OSError, ValueError):
            pass
        return None
    diagnostics = StartupDiagnostics(stream)
    try:
        # Arm first: even diagnostic logging or subsequent imports can block.
        faulthandler.dump_traceback_later(
            interval, repeat=True, file=stream, exit=False
        )
        diagnostics._timer.start()
        _marker(stream, "module_import")
    except (OSError, ValueError, RuntimeError):
        diagnostics.close()
        return None
    atexit.register(diagnostics.close)
    return diagnostics
