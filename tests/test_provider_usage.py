"""Contracts for normalized provider account usage."""

from datetime import datetime, timezone

import pytest

from drover.server.providers.types import (
    ProviderAccountSnapshot,
    ProviderUsageWindow,
    provider_snapshot_table,
)


def test_provider_window_rejects_negative_percent():
    with pytest.raises(ValueError, match="used_percent"):
        ProviderUsageWindow(kind="primary", used_percent=-1, resets_at=None)


def test_provider_window_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="starts_at"):
        ProviderUsageWindow(
            kind="primary",
            used_percent=25,
            starts_at=datetime(2026, 8, 8, 10, 0),
        )


def test_provider_snapshot_rejects_naive_observation_time():
    with pytest.raises(ValueError, match="observed_at"):
        ProviderAccountSnapshot(
            snapshot_id="snapshot-1",
            dedup_key="dedup-1",
            provider="codex",
            account_label="Personal",
            plan_label=None,
            host_id="mac-mini",
            status="ok",
            observed_at=datetime(2026, 8, 8, 10, 0),
            windows=(),
            source="codex-cli",
        )


def test_provider_snapshot_table_preserves_each_usage_window():
    observed_at = datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc)
    snapshot = ProviderAccountSnapshot(
        snapshot_id="snapshot-1",
        dedup_key="dedup-1",
        provider="codex",
        account_label="Personal",
        plan_label="Pro",
        host_id="mac-mini",
        status="ok",
        observed_at=observed_at,
        windows=(
            ProviderUsageWindow(kind="primary", used_percent=25.0),
            ProviderUsageWindow(
                kind="secondary",
                used_percent=60.0,
                window_minutes=10080,
            ),
        ),
        source="codex-cli",
    )

    rows = provider_snapshot_table(snapshot).to_pylist()

    assert [(row["window_kind"], row["used_percent"]) for row in rows] == [
        ("primary", 25.0),
        ("secondary", 60.0),
    ]
