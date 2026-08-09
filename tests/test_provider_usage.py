"""Contracts for normalized provider account usage."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import duckdb
import pytest

from drover.schema import bootstrap
from drover.config import load_config
from drover.server.providers.codex import CodexUsageProbe
from drover.server.cockpit.analytics import AnalyticsFilters
from drover.server.cockpit.service import CockpitService, ProviderRefreshLoop
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

CODEX_ERROR_PAYLOAD = {
    "accounts": [
        {
            "snapshot_id": "snapshot-error",
            "dedup_key": "dedup-error",
            "provider": "openai",
            "account_label": "Codex",
            "plan_label": None,
            "host_id": "mac-mini",
            "usage_status": "supported",
            "status": "error",
            "observed_at": "2026-08-08T10:05:00+00:00",
            "source": "codex-app-server",
            "error_category": "timeout",
            "windows": [],
        }
    ],
    "observed_at": "2026-08-08T10:05:00+00:00",
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


def test_codex_probe_separates_a_missing_cli_from_a_host_failure(tmp_path):
    # "unavailable" is also the host-level catch-all, so a CLI that is simply
    # not on the daemon's PATH needs its own category to stay actionable.
    snapshot = CodexUsageProbe(
        command=(str(tmp_path / "definitely-not-installed"), "app-server", "--stdio")
    ).read()

    assert snapshot.status == "error"
    assert snapshot.error_category == "cli_not_found"


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


def test_codex_error_before_account_discovery_stales_existing_account(
    provider_service, provider_host
):
    provider_service.refresh_host(provider_host, fetch=lambda _: GOOD_PAYLOAD)
    provider_service.refresh_host(provider_host, fetch=lambda _: CODEX_ERROR_PAYLOAD)

    accounts = provider_service.latest_accounts()

    assert len(accounts) == 1
    assert accounts[0].account_label == "person@example.com"
    assert accounts[0].status == "stale"
    assert accounts[0].error_category == "timeout"
    assert accounts[0].windows[0].used_percent == 25.0


def test_empty_provider_inventory_stales_existing_host_accounts(
    provider_service, provider_host
):
    provider_service.refresh_host(provider_host, fetch=lambda _: GOOD_PAYLOAD)
    provider_service.refresh_host(
        provider_host,
        fetch=lambda _: {
            "accounts": [],
            "observed_at": "2026-08-08T10:05:00+00:00",
        },
    )

    accounts = provider_service.latest_accounts()

    assert len(accounts) == 1
    assert accounts[0].account_label == "person@example.com"
    assert accounts[0].status == "stale"
    assert accounts[0].error_category == "empty_inventory"


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


def test_identical_success_advances_effective_freshness_without_new_snapshot(
    tmp_path, provider_host
):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    clock = [datetime(2026, 8, 8, 10, 1, tzinfo=timezone.utc)]
    service = ProviderUsageService(
        duckdb_path,
        parquet_dir,
        clock=lambda: clock[0],
        freshness_threshold_seconds=300,
    )

    service.refresh_host(provider_host, fetch=lambda _: GOOD_PAYLOAD)
    clock[0] += timedelta(minutes=2)
    service.refresh_host(provider_host, fetch=lambda _: GOOD_PAYLOAD)

    account = service.latest_accounts()
    parts = list((parquet_dir / "provider_usage_snapshots").glob("*.parquet"))
    with duckdb.connect(str(duckdb_path)) as con:
        snapshot_count = con.execute(
            "SELECT count(DISTINCT snapshot_id) FROM provider_usage_snapshots"
        ).fetchone()[0]

    assert len(account) == 1
    assert account[0].status == "ok"
    assert account[0].observed_at == clock[0]
    assert account[0].provider_observed_at == datetime(
        2026, 8, 8, 10, tzinfo=timezone.utc
    )
    assert account[0].freshness_age_seconds == 0
    assert len(parts) == 1
    assert snapshot_count == 1

    overview = CockpitService(
        duckdb_path=None,
        provider_usage=service,
        connect=lambda: (_ for _ in ()).throw(RuntimeError("activity unavailable")),
    ).overview(AnalyticsFilters(days=7))
    capacity = overview["provider_capacity"]
    assert capacity["status"] == "ok"
    assert capacity["observed_at"] == clock[0]
    assert capacity["data"][0]["provider_observed_at"] == datetime(
        2026, 8, 8, 10, tzinfo=timezone.utc
    )


def test_success_older_than_injected_threshold_is_explicitly_stale(
    tmp_path, provider_host
):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    clock = [datetime(2026, 8, 8, 10, 1, tzinfo=timezone.utc)]
    service = ProviderUsageService(
        duckdb_path,
        parquet_dir,
        clock=lambda: clock[0],
        freshness_threshold_seconds=300,
    )
    service.refresh_host(provider_host, fetch=lambda _: GOOD_PAYLOAD)

    clock[0] += timedelta(seconds=301)
    account = service.latest_accounts()[0]

    assert account.status == "stale"
    assert account.error_category == "freshness_expired"
    assert account.freshness_age_seconds == 301


def test_success_within_threshold_is_not_falsely_stale(tmp_path, provider_host):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    clock = [datetime(2026, 8, 8, 10, 1, tzinfo=timezone.utc)]
    service = ProviderUsageService(
        duckdb_path,
        parquet_dir,
        clock=lambda: clock[0],
        freshness_threshold_seconds=300,
    )
    service.refresh_host(provider_host, fetch=lambda _: GOOD_PAYLOAD)

    clock[0] += timedelta(seconds=299)
    account = service.latest_accounts()[0]

    assert account.status == "ok"
    assert account.error_category is None
    assert account.freshness_age_seconds == 299


def test_expired_provider_reported_window_marks_account_stale(tmp_path, provider_host):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    clock = [datetime(2026, 8, 8, 10, 1, tzinfo=timezone.utc)]
    service = ProviderUsageService(
        duckdb_path,
        parquet_dir,
        clock=lambda: clock[0],
        freshness_threshold_seconds=86_400,
    )
    service.refresh_host(provider_host, fetch=lambda _: GOOD_PAYLOAD)

    clock[0] = datetime(2026, 8, 8, 15, 0, 1, tzinfo=timezone.utc)
    account = service.latest_accounts()[0]

    assert account.status == "stale"
    assert account.error_category == "provider_window_expired"
    assert account.windows[0].resets_at == datetime(2026, 8, 8, 15, tzinfo=timezone.utc)


def test_offline_host_stales_immediately_and_recovery_clears_status(
    tmp_path, provider_host
):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    service = ProviderUsageService(
        duckdb_path,
        parquet_dir,
        clock=lambda: datetime(2026, 8, 8, 10, 1, tzinfo=timezone.utc),
        freshness_threshold_seconds=300,
    )
    host = SimpleNamespace(**vars(provider_host), status="online")

    class _Registry:
        def list_hosts(self):
            return [host]

    refreshes = []
    monotonic_clock = [0.0]
    loop = ProviderRefreshLoop(
        provider_usage=service,
        registry=_Registry(),
        shutdown_event=threading.Event(),
        interval_seconds=300,
        clock=lambda: monotonic_clock[0],
        fetch=lambda item: refreshes.append(item.host_id) or GOOD_PAYLOAD,
    )

    loop.run_once()
    host.status = "offline"
    monotonic_clock[0] = 10
    loop.run_once()
    offline = service.latest_accounts()[0]
    host.status = "online"
    monotonic_clock[0] = 20
    loop.run_once()
    recovered = service.latest_accounts()[0]

    assert offline.status == "stale"
    assert offline.error_category == "host_offline"
    assert recovered.status == "ok"
    assert recovered.error_category is None
    assert refreshes == ["mac-mini", "mac-mini"]


@pytest.mark.parametrize(("toml_value", "expected"), [("900", 900.0), ("900.5", 900.5)])
def test_provider_freshness_threshold_is_runtime_configurable(
    tmp_path, toml_value, expected
):
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"[provider]\nfreshness_threshold_seconds = {toml_value}\n")

    config = load_config(config_path)

    assert config.provider_freshness_threshold_seconds == expected


@pytest.mark.parametrize(
    "toml_value",
    ["nan", "inf", "+inf", "-inf", "true", "false", "0", "-1", '"300"'],
)
def test_config_rejects_non_positive_or_non_finite_provider_freshness_thresholds(
    tmp_path, toml_value
):
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"[provider]\nfreshness_threshold_seconds = {toml_value}\n")

    with pytest.raises(ValueError, match="finite positive number"):
        load_config(config_path)


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), -float("inf"), True, False, 0, -1, "300"]
)
def test_provider_service_rejects_invalid_freshness_thresholds(tmp_path, value):
    with pytest.raises(ValueError, match="finite positive number"):
        ProviderUsageService(
            tmp_path / "drover.duckdb",
            tmp_path / "parquet",
            freshness_threshold_seconds=value,
        )


@pytest.mark.parametrize("value", [1, 300.5])
def test_provider_service_accepts_finite_positive_numeric_thresholds(tmp_path, value):
    service = ProviderUsageService(
        tmp_path / "drover.duckdb",
        tmp_path / "parquet",
        freshness_threshold_seconds=value,
    )

    assert service.freshness_threshold_seconds == float(value)


def test_legacy_codex_source_is_normalized_to_canonical_contract(
    tmp_path, provider_host
):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    service = ProviderUsageService(
        duckdb_path,
        parquet_dir,
        clock=lambda: datetime(2026, 8, 8, 10, 1, tzinfo=timezone.utc),
    )
    legacy_payload = {
        **GOOD_PAYLOAD,
        "accounts": [{**GOOD_PAYLOAD["accounts"][0], "source": "codex_app_server"}],
    }

    service.refresh_host(provider_host, fetch=lambda _: legacy_payload)

    assert service.latest_accounts()[0].source == "codex-app-server"
