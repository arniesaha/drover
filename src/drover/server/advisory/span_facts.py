"""Whether advisory fact loaders may read span Parquet (#473).

Spans are an optional integration and off by default. Operational facts are
read from several places -- the worker, Check Again, and a child process --
so the decision is one process-wide setting made at startup and forwarded to
the child explicitly, rather than a parameter threaded through each path.
"""

from __future__ import annotations

_enabled = False


def configure(enabled: bool) -> None:
    global _enabled
    _enabled = enabled is True


def enabled() -> bool:
    return _enabled
