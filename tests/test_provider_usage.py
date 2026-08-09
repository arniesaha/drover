"""Contracts for normalized provider account usage."""

from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.providers.codex import CodexUsageProbe
from drover.server.providers.inventory import detect_provider_accounts
from drover.server.providers.service import ProviderUsageService
from drover.server.providers.types import (
    ProviderAccountSnapshot,
    ProviderUsageWindow,
    provider_snapshot_table,
)

GOOD_PAYLOAD = {
    "accounts": [
        {
            "snapshot_id": "snapshot-good",
            "dedup_key": "dedup-good",
            "provider": "openai",
            "account_label": "person@example.com",
            "plan_label": "plus",
            "host_id": "mac-mini",
            "usage_status": "supported",
            "status": "ok",
            "observed_at": "2026-08-08T10:00:00+00:00",
            "source": "codex-app-server",
            "error_category": None,
            "windows": [
                {
                    "kind": "primary",
                    "used_percent": 25.0,
                    "limit_value": None,
                    "remaining_value": None,
                    "unit": None,
                    "window_minutes": 300,
                    "starts_at": None,
                    "resets_at": "2026-08-08T15:00:00+00:00",
                }
            ],
        }
    ],
    "observed_at": "2026-08-08T10:00:00+00:00",
}


@pytest.fixture
def provider_service(tmp_path):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return ProviderUsageService(duckdb_path, parquet_dir)


@pytest.fixture
def provider_host():
    return SimpleNamespace(
        host_id="mac-mini",
        local_url="http://127.0.0.1:7081",
        tailscale_url=None,
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


def test_last_good_provider_snapshot_survives_refresh_failure(
    provider_service, provider_host
):
    provider_service.refresh_host(provider_host, fetch=lambda _: GOOD_PAYLOAD)
    provider_service.refresh_host(
        provider_host,
        fetch=lambda _: (_ for _ in ()).throw(TimeoutError()),
    )

    account = provider_service.latest_accounts()[0]

    assert account.status == "stale"
    assert account.error_category == "timeout"
    assert account.windows[0].used_percent == 25.0


def test_provider_refresh_is_atomic_deduplicated_and_records_every_attempt(
    provider_service, provider_host
):
    provider_service.refresh_host(provider_host, fetch=lambda _: GOOD_PAYLOAD)
    provider_service.refresh_host(provider_host, fetch=lambda _: GOOD_PAYLOAD)

    parts = list(
        (provider_service.parquet_dir / "provider_usage_snapshots").glob("*.parquet")
    )
    temporary_parts = list(
        (provider_service.parquet_dir / "provider_usage_snapshots").glob("*.tmp")
    )
    con = duckdb.connect(str(provider_service.duckdb_path))
    try:
        snapshot_count = con.execute(
            "SELECT count(DISTINCT snapshot_id) FROM provider_usage_snapshots"
        ).fetchone()[0]
        connection = con.execute("""
            SELECT last_attempt_at, last_success_at, error_category
            FROM provider_connections
            WHERE provider = 'openai'
              AND account_label = 'person@example.com'
              AND host_id = 'mac-mini'
            """).fetchone()
    finally:
        con.close()

    assert len(parts) == 1
    assert temporary_parts == []
    assert snapshot_count == 1
    assert connection[0] is not None
    assert connection[1] is not None
    assert connection[2] is None
