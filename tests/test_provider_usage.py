"""Contracts for normalized provider account usage."""

from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from drover.server.providers.codex import CodexUsageProbe
from drover.server.providers.inventory import detect_provider_accounts
from drover.server.providers.types import (
    ProviderAccountSnapshot,
    ProviderUsageWindow,
    provider_snapshot_table,
)


@pytest.fixture
def fake_codex_app_server(tmp_path):
    """A JSONL app-server double exercising the public RPC boundary."""
    script = Path(tmp_path) / "fake_codex_app_server.py"
    script.write_text("""\
import json
import sys
import time

mode = sys.argv[1]
for line in sys.stdin:
    request = json.loads(line)
    if mode == "timeout":
        print("Authorization: Bearer top-secret-token", file=sys.stderr, flush=True)
        time.sleep(30)
    if mode == "stderr_flood":
        print("diagnostic " * 20000, file=sys.stderr, flush=True)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {"userAgent": "fake"}
    elif request["method"] == "account/read":
        result = {
            "account": {
                "type": "chatgpt",
                "email": "person@example.com",
                "planType": "plus",
            },
            "requiresOpenaiAuth": True,
        }
    elif request["method"] == "account/rateLimits/read":
        result = {
            "rateLimits": {
                "primary": {
                    "usedPercent": 25,
                    "windowDurationMins": 15,
                    "resetsAt": 1730947200,
                },
                "secondary": {
                    "usedPercent": 60,
                    "windowDurationMins": 10080,
                    "resetsAt": 1731552000,
                },
            }
        }
    else:
        continue
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
""")
    return SimpleNamespace(
        command=(sys.executable, "-u", str(script), "success"),
        timeout_command=(sys.executable, "-u", str(script), "timeout"),
        noisy_command=(sys.executable, "-u", str(script), "stderr_flood"),
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


def test_provider_snapshot_rejects_missing_observation_time():
    with pytest.raises(ValueError, match="observed_at"):
        ProviderAccountSnapshot(
            snapshot_id="snapshot-1",
            dedup_key="dedup-1",
            provider="codex",
            account_label="Personal",
            plan_label=None,
            host_id="mac-mini",
            status="ok",
            observed_at=None,
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


def test_codex_probe_reads_plan_and_multiple_windows(fake_codex_app_server):
    snapshot = CodexUsageProbe(command=fake_codex_app_server.command).read()

    assert snapshot.provider == "openai"
    assert snapshot.plan_label == "plus"
    assert snapshot.account_label == "person@example.com"
    assert [(window.kind, window.used_percent) for window in snapshot.windows] == [
        ("primary", 25.0),
        ("secondary", 60.0),
    ]
    assert snapshot.windows[0].resets_at == datetime(
        2024, 11, 7, 2, 40, tzinfo=timezone.utc
    )


def test_codex_probe_times_out_without_exposing_stderr(fake_codex_app_server):
    snapshot = CodexUsageProbe(
        command=fake_codex_app_server.timeout_command, timeout_s=0.05
    ).read()

    assert snapshot.status == "error"
    assert snapshot.error_category == "timeout"
    assert "top-secret-token" not in (snapshot.error_category or "")


def test_codex_probe_does_not_block_on_noisy_stderr(fake_codex_app_server):
    snapshot = CodexUsageProbe(
        command=fake_codex_app_server.noisy_command, timeout_s=1
    ).read()

    assert snapshot.status == "ok"
    assert snapshot.plan_label == "plus"


def test_detected_gemini_is_honest_when_quota_contract_is_unavailable():
    accounts = detect_provider_accounts({"harnesses": [{"id": "gemini"}]})

    assert accounts[0].provider == "google"
    assert accounts[0].usage_status == "usage_unavailable"


def test_inventory_omits_disabled_harnesses_and_keeps_supported_codex():
    accounts = detect_provider_accounts(
        {
            "host_id": "mac-mini",
            "harnesses": [
                {"name": "codex", "enabled": True},
                {"name": "claude-code", "enabled": False},
            ],
        }
    )

    assert [(account.provider, account.usage_status) for account in accounts] == [
        ("openai", "supported")
    ]
    assert accounts[0].host_id == "mac-mini"
