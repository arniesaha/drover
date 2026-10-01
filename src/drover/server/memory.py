"""Best-effort analytical memory diagnostics; never borrow another owner's cursor."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections import OrderedDict

log = logging.getLogger("drover.memory")
_guard = threading.Lock()
_last_release = float("-inf")
_samples: OrderedDict[str, float] = OrderedDict()
_SAMPLE_SECONDS = 60.0
_MAX_INSTANCES = 64


def process_rss_bytes() -> int:
    """Current RSS, not resource.getrusage's lifetime high-water mark.

    ps reports KiB on both supported server platforms (macOS and Linux).
    Called only for opt-in diagnostics, with a bounded subprocess lifetime.
    """
    return (
        int(
            subprocess.check_output(
                ["ps", "-o", "rss=", "-p", str(os.getpid())],
                text=True,
                timeout=1,
            ).strip()
        )
        * 1024
    )


def log_instance_memory(con, path: str) -> None:
    """Sample an owner's idle connection immediately before it closes.

    One sample per file per minute, at most 64 remembered paths. DuckDB's
    memory_usage_bytes includes attached catalogs in this instance; it excludes
    Python/Arrow allocations and other instances. Sampling failure is harmless.
    """
    if os.environ.get("DROVER_DUCKDB_MEMORY_DIAGNOSTICS") != "1":
        return
    now = time.monotonic()
    with _guard:
        if now - _samples.get(path, float("-inf")) < _SAMPLE_SECONDS:
            return
        _samples[path] = now
        _samples.move_to_end(path)
        while len(_samples) > _MAX_INSTANCES:
            _samples.popitem(last=False)
    try:
        used, spilled = con.execute(
            "SELECT coalesce(sum(memory_usage_bytes), 0), "
            "coalesce(sum(temporary_storage_bytes), 0) FROM duckdb_memory()"
        ).fetchone()
        log.info(
            "analytical_memory pid=%d instance=%s rss_bytes=%d "
            "duckdb_memory_bytes=%d duckdb_spill_bytes=%d phase=before_close",
            os.getpid(),
            path,
            process_rss_bytes(),
            used,
            spilled,
        )
    except Exception:
        log.debug("memory sample unavailable for %s", path, exc_info=True)


def release_idle_arrow_memory() -> None:
    """Return unused pool pages after analytical requests, at most once/minute.

    No GC and no closing another thread's handles: live Arrow buffers remain
    owned by their users. Do not import Arrow on a request that never used it.
    Allocator release is best effort, not a promise that RSS equals DuckDB use.
    """
    global _last_release
    arrow = sys.modules.get("pyarrow")
    if arrow is None:
        return
    now = time.monotonic()
    with _guard:
        if now - _last_release < _SAMPLE_SECONDS:
            return
        _last_release = now
    try:
        arrow.default_memory_pool().release_unused()
    except Exception:
        log.debug("could not release unused Arrow pool pages", exc_info=True)
