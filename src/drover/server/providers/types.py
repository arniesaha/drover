"""Strict normalized records for provider account usage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from numbers import Real
from typing import Literal

import pyarrow as pa


def _require_timezone_aware(value: datetime | None, field_name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True)
class ProviderUsageWindow:
    kind: str
    used_percent: float | None
    limit_value: float | None = None
    remaining_value: float | None = None
    unit: str | None = None
    window_minutes: int | None = None
    starts_at: datetime | None = None
    resets_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.used_percent is not None and (
            isinstance(self.used_percent, bool)
            or not isinstance(self.used_percent, Real)
            or not math.isfinite(self.used_percent)
            or not 0 <= self.used_percent <= 100
        ):
            raise ValueError("used_percent must be within [0, 100]")
        _require_timezone_aware(self.starts_at, "starts_at")
        _require_timezone_aware(self.resets_at, "resets_at")


@dataclass(frozen=True)
class ProviderAccountSnapshot:
    snapshot_id: str
    dedup_key: str
    provider: str
    account_label: str
    plan_label: str | None
    host_id: str
    status: Literal["ok", "usage_unavailable", "stale", "error"]
    observed_at: datetime
    windows: tuple[ProviderUsageWindow, ...]
    source: str
    error_category: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"ok", "usage_unavailable", "stale", "error"}:
            raise ValueError("status must be a supported provider status")
        _require_timezone_aware(self.observed_at, "observed_at")
        windows = tuple(self.windows)
        if not all(isinstance(window, ProviderUsageWindow) for window in windows):
            raise ValueError("windows must contain ProviderUsageWindow records")
        object.__setattr__(self, "windows", windows)


def provider_snapshot_schema() -> pa.Schema:
    """Arrow schema for one flattened provider usage-window record."""
    return pa.schema(
        [
            ("snapshot_id", pa.string()),
            ("dedup_key", pa.string()),
            ("provider", pa.string()),
            ("account_label", pa.string()),
            ("plan_label", pa.string()),
            ("host_id", pa.string()),
            ("status", pa.string()),
            ("observed_at", pa.timestamp("us", tz="UTC")),
            ("source", pa.string()),
            ("error_category", pa.string()),
            ("window_kind", pa.string()),
            ("used_percent", pa.float64()),
            ("limit_value", pa.float64()),
            ("remaining_value", pa.float64()),
            ("unit", pa.string()),
            ("window_minutes", pa.int64()),
            ("starts_at", pa.timestamp("us", tz="UTC")),
            ("resets_at", pa.timestamp("us", tz="UTC")),
        ]
    )


def provider_snapshot_table(snapshot: ProviderAccountSnapshot) -> pa.Table:
    """Flatten a snapshot into one Arrow row for every reported usage window.

    Usage-unavailable snapshots retain one null-window row so connector state is
    observable even when a provider reports no quota windows.
    """
    windows: tuple[ProviderUsageWindow | None, ...] = snapshot.windows or (None,)
    rows = [
        {
            "snapshot_id": snapshot.snapshot_id,
            "dedup_key": snapshot.dedup_key,
            "provider": snapshot.provider,
            "account_label": snapshot.account_label,
            "plan_label": snapshot.plan_label,
            "host_id": snapshot.host_id,
            "status": snapshot.status,
            "observed_at": snapshot.observed_at,
            "source": snapshot.source,
            "error_category": snapshot.error_category,
            "window_kind": window.kind if window else None,
            "used_percent": window.used_percent if window else None,
            "limit_value": window.limit_value if window else None,
            "remaining_value": window.remaining_value if window else None,
            "unit": window.unit if window else None,
            "window_minutes": window.window_minutes if window else None,
            "starts_at": window.starts_at if window else None,
            "resets_at": window.resets_at if window else None,
        }
        for window in windows
    ]
    return pa.Table.from_pylist(rows, schema=provider_snapshot_schema())
