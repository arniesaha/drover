"""Best-effort analytical memory diagnostics; never borrow another owner's cursor."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque

log = logging.getLogger("drover.memory")
_guard = threading.Lock()
_last_release = float("-inf")
_samples: OrderedDict[str, float] = OrderedDict()
_SAMPLE_SECONDS = 60.0
_MAX_INSTANCES = 64
_sample_times: deque[float] = deque()
_MAX_SAMPLES_PER_MINUTE = 64


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

    One sample per file per minute, at most 64 samples per process per minute.
    Remember only 64 hashed identities; never log paths or exception payloads.
    DuckDB's memory_usage_bytes includes attached catalogs in this instance;
    it excludes Python/Arrow allocations and other instances. Sampling failure is harmless.
    """
    if os.environ.get("DROVER_DUCKDB_MEMORY_DIAGNOSTICS") != "1":
        return
    instance = hashlib.sha256(path.encode()).hexdigest()[:16]
    now = time.monotonic()
    with _guard:
        while _sample_times and now - _sample_times[0] >= _SAMPLE_SECONDS:
            _sample_times.popleft()
        if len(_sample_times) >= _MAX_SAMPLES_PER_MINUTE:
            return
        if now - _samples.get(instance, float("-inf")) < _SAMPLE_SECONDS:
            return
        _samples[instance] = now
        _samples.move_to_end(instance)
        _sample_times.append(now)
        while len(_samples) > _MAX_INSTANCES:
            _samples.popitem(last=False)
    try:
        rows = con.execute(
            "SELECT tag, memory_usage_bytes, temporary_storage_bytes "
            "FROM duckdb_memory()"
        ).fetchall()
        used = sum(row[1] for row in rows)
        spilled = sum(row[2] for row in rows)
        tags = {row[0]: row[1] for row in rows if row[1]}
        # Observe pools already loaded by the owner; never import Arrow or
        # start tracing on a runtime request. These counts are not additive
        # with RSS and do not cover all native allocations.
        arrow = sys.modules.get("pyarrow")
        arrow_bytes = arrow.total_allocated_bytes() if arrow is not None else 0
        tracing = sys.modules.get("tracemalloc")
        traced = (
            tracing.get_traced_memory()[0]
            if tracing is not None and tracing.is_tracing()
            else None
        )
        log.info(
            "analytical_memory pid=%d instance=%s rss_bytes=%d "
            "duckdb_memory_bytes=%d duckdb_spill_bytes=%d phase=before_close "
            "arrow_pool_bytes=%d python_traced_bytes=%s duckdb_tags=%s",
            os.getpid(),
            instance,
            process_rss_bytes(),
            used,
            spilled,
            arrow_bytes,
            traced,
            json.dumps(tags, separators=(",", ":"), sort_keys=True),
        )
    except Exception:
        log.debug("memory sample unavailable for instance=%s", instance)


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


_READER_SAMPLE = re.compile(
    r"INFO:drover\.memory:(analytical_memory pid=\d{1,20} instance=[0-9a-f]{16} "
    r"rss_bytes=\d{1,20} duckdb_memory_bytes=\d{1,20} "
    r"duckdb_spill_bytes=\d{1,20} phase=before_close "
    r"arrow_pool_bytes=\d{1,20} python_traced_bytes=(?:None|\d{1,20}) "
    r'duckdb_tags=\{(?:"[A-Z_]{1,32}":\d{1,20}(?:,"[A-Z_]{1,32}":\d{1,20})*)?\})'
)


def reader_memory_samples(stderr: str) -> list[str]:
    """Forward only bounded numeric memory records, never arbitrary child stderr."""
    samples = []
    for line in stderr[:65536].splitlines()[:64]:
        if len(line) > 4096:
            continue
        match = _READER_SAMPLE.fullmatch(line)
        if match is not None:
            samples.append(match.group(1))
    return samples
