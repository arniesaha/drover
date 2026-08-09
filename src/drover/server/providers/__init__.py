"""Normalized provider account and usage contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import ProviderAccountSnapshot, ProviderUsageWindow

__all__ = [
    "ProviderAccountSnapshot",
    "ProviderUsageWindow",
    "provider_snapshot_table",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    from .types import (
        ProviderAccountSnapshot,
        ProviderUsageWindow,
        provider_snapshot_table,
    )

    values = {
        "ProviderAccountSnapshot": ProviderAccountSnapshot,
        "ProviderUsageWindow": ProviderUsageWindow,
        "provider_snapshot_table": provider_snapshot_table,
    }
    return values[name]
