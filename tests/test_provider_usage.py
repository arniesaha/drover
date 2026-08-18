"""Contracts for normalized provider account usage."""

import logging
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from drover.config import load_config
from drover.schema import bootstrap
from drover.server.cockpit.analytics import AnalyticsFilters
from drover.server.cockpit.service import CockpitService, ProviderRefreshLoop
from drover.server.harness.models import HarnessHost
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


def _relabelled_payload(payload, *, snapshot_id, dedup_key):
    account = {
        **payload["accounts"][0],
        "snapshot_id": snapshot_id,
        "dedup_key": dedup_key,
    }
    return {**payload, "accounts": [account]}


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
    if mode == "stdout_flood":
        print(json.dumps({"id": request["id"], "result": {"blob": "x" * 1100000}}), flush=True)
        continue
    if mode == "stdout_multibyte_flood":
        payload = json.dumps(
            {"id": request["id"], "result": {"blob": "é" * 600000}},
            ensure_ascii=False,
        ).encode("utf-8")
        sys.stdout.buffer.write(payload + b"\\n")
        sys.stdout.buffer.flush()
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
    elif request["method"] == "model/list":
        result = {
            "data": [
                {
                    "model": "gpt-5.6-terra",
                    "displayName": "GPT-5.6 Terra",
                }
            ],
            "nextCursor": None,
        }
    else:
        continue
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
""")
    return SimpleNamespace(
        command=(sys.executable, "-u", str(script), "success"),
        timeout_command=(sys.executable, "-u", str(script), "timeout"),
        noisy_command=(sys.executable, "-u", str(script), "stderr_flood"),
        oversized_command=(sys.executable, "-u", str(script), "stdout_flood"),
        oversized_multibyte_command=(
            sys.executable,
            "-u",
            str(script),
            "stdout_multibyte_flood",
        ),
    )


def test_provider_refresh_retires_a_label_the_host_stopped_reporting(
    provider_service, provider_host
):
    # A probe that fails before it can read the account falls back to the
    # "Codex" label, which becomes its own identity. Once the host starts
    # reporting the real account, the fallback must not linger as a second
    # card frozen at the failure.
    provider_service.refresh_host(provider_host, fetch=lambda host: CODEX_ERROR_PAYLOAD)
    provider_service.refresh_host(provider_host, fetch=lambda host: GOOD_PAYLOAD)

    openai = [
        account
        for account in provider_service.latest_accounts()
        if account.provider == "openai"
    ]

    assert [account.account_label for account in openai] == ["person@example.com"]


def test_provider_refresh_restores_a_retired_label_the_host_reports_again(
    provider_service, provider_host
):
    # Retirement follows the host, it is not a tombstone: an account that comes
    # back (a re-login under the earlier identity) has to project again.
    provider_service.refresh_host(provider_host, fetch=lambda host: CODEX_ERROR_PAYLOAD)
    provider_service.refresh_host(provider_host, fetch=lambda host: GOOD_PAYLOAD)

    returning = _relabelled_payload(
        {
            **GOOD_PAYLOAD,
            "accounts": [{**GOOD_PAYLOAD["accounts"][0], "account_label": "Codex"}],
        },
        snapshot_id="snapshot-returning",
        dedup_key="dedup-returning",
    )
    provider_service.refresh_host(provider_host, fetch=lambda host: returning)

    labels = {
        account.account_label
        for account in provider_service.latest_accounts()
        if account.provider == "openai"
    }

    assert labels == {"Codex"}


def test_provider_refresh_retires_only_within_the_reporting_host(provider_service):
    mac_mini = SimpleNamespace(
        host_id="mac-mini", local_url="http://127.0.0.1:7081", tailscale_url=None
    )
    nas = SimpleNamespace(
        host_id="nas", local_url="http://127.0.0.1:7082", tailscale_url=None
    )
    provider_service.refresh_host(nas, fetch=lambda host: CODEX_ERROR_PAYLOAD)
    provider_service.refresh_host(
        mac_mini,
        fetch=lambda host: _relabelled_payload(
            GOOD_PAYLOAD, snapshot_id="snapshot-mac", dedup_key="dedup-mac"
        ),
    )

    labels = {
        (account.host_id, account.account_label)
        for account in provider_service.latest_accounts()
        if account.provider == "openai"
    }

    # One host reporting a real account says nothing about another host.
    assert labels == {("mac-mini", "person@example.com"), ("nas", "Codex")}


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


def test_codex_app_server_session_initializes_once_and_calls_multiple_methods(
    fake_codex_app_server,
):
    from drover.server.providers.codex_app_server import CodexAppServerSession

    with CodexAppServerSession(fake_codex_app_server.command, timeout_s=1) as client:
        account = client.request("account/read", {"refreshToken": False})
        models = client.request(
            "model/list", {"cursor": None, "includeHidden": False, "limit": 100}
        )

    assert account["account"]["email"] == "person@example.com"
    assert models["data"][0]["model"] == "gpt-5.6-terra"


def test_codex_app_server_transport_imports_in_a_fresh_process():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from drover.server.providers.codex_app_server import "
            "CodexAppServerSession",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_codex_app_server_session_rejects_oversized_stdout(
    fake_codex_app_server,
):
    from drover.server.providers.codex_app_server import (
        CodexAppServerError,
        CodexAppServerSession,
    )

    with pytest.raises(CodexAppServerError, match="protocol_error"):
        with CodexAppServerSession(
            fake_codex_app_server.oversized_command, timeout_s=1
        ):
            pass


def test_codex_app_server_session_applies_stdout_limit_to_encoded_bytes(
    fake_codex_app_server,
):
    from drover.server.providers.codex_app_server import (
        CodexAppServerError,
        CodexAppServerSession,
    )

    with pytest.raises(CodexAppServerError, match="protocol_error"):
        with CodexAppServerSession(
            fake_codex_app_server.oversized_multibyte_command, timeout_s=1
        ):
            pass


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


def test_codex_probe_kill_path_never_raises():
    """``_stop_process`` swallows a SIGKILL that does not take.

    Cleanup runs in ``read()``'s ``finally``, where an exception escapes the
    method rather than being caught by its own ``except`` clauses.
    ``_provider_usage`` calls the probe with no ``try`` and ``harnessd``'s
    ``do_GET`` has no wrapper, so anything escaping here means no HTTP
    response at all -- the Claude and Google cards go down with a Codex
    problem (drover#65).
    """
    from drover.server.providers.codex import _stop_process

    class _Stuck:
        stdin = None
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)

    _stop_process(_Stuck())


def test_codex_probe_close_failure_never_raises():
    """Closing stdin on a dead child raises BrokenPipeError; cleanup eats it."""
    from drover.server.providers.codex import _stop_process

    class _BrokenStdin:
        def close(self):
            raise BrokenPipeError(32, "Broken pipe")

    class _Dead:
        stdin = _BrokenStdin()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    _stop_process(_Dead())


def test_codex_probe_failure_is_logged_with_its_category(caplog, tmp_path):
    """A transient failure must leave a trace naming the category.

    Without this a card flips to ``error`` and recovers with nothing recorded,
    so there is nothing to diagnose afterwards.
    """
    with caplog.at_level(logging.WARNING, logger="drover.server.providers.codex"):
        snapshot = CodexUsageProbe(
            command=(
                str(tmp_path / "definitely-not-installed"),
                "app-server",
                "--stdio",
            )
        ).read(host_id="mac-mini")

    assert snapshot.error_category == "cli_not_found"
    assert any("cli_not_found" in record.message for record in caplog.records)


def test_detected_agy_is_honest_when_quota_contract_is_unavailable():
    accounts = detect_provider_accounts({"harnesses": [{"id": "agy"}]})

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


def test_claude_inventories_as_supported():
    detected = detect_provider_accounts(
        {"host_id": "mac-mini", "harnesses": ["claude-code", "agy"]}
    )
    by_provider = {d.provider: d for d in detected}

    assert by_provider["anthropic"].usage_status == "supported"
    assert by_provider["google"].usage_status == "usage_unavailable"


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

    # A real HarnessHost, not a namespace double: the refresh loop calls
    # is_stale() on whatever the registry yields, and the registry only ever
    # yields these. last_seen_at stays fresh so this test isolates status.
    def _host(status):
        return HarnessHost(
            host_id=provider_host.host_id,
            display_name="Mac Mini",
            kind="macos",
            status=status,
            connection_kind="direct",
            local_url=provider_host.local_url,
            tailscale_url=provider_host.tailscale_url,
            last_seen_at=datetime.now(timezone.utc),
        )

    current = [_host("online")]

    class _Registry:
        def list_hosts(self):
            return [current[0]]

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
    current[0] = _host("offline")
    monotonic_clock[0] = 10
    loop.run_once()
    offline = service.latest_accounts()[0]
    current[0] = _host("online")
    monotonic_clock[0] = 20
    loop.run_once()
    recovered = service.latest_accounts()[0]

    assert offline.status == "stale"
    assert offline.error_category == "host_offline"
    assert recovered.status == "ok"
    assert recovered.error_category is None
    assert refreshes == ["mac-mini", "mac-mini"]


def test_provider_refresh_loop_skips_stale_host(tmp_path):
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=tmp_path / "drover.duckdb")
    service = ProviderUsageService(
        duckdb_path=tmp_path / "drover.duckdb",
        parquet_dir=tmp_path / "parquet",
    )
    now = datetime.now(timezone.utc)
    fresh_host = HarnessHost(
        host_id="gpu-pc",
        display_name="GPU PC",
        kind="linux",
        status="online",
        connection_kind="direct",
        last_seen_at=now,
    )
    stale_host = HarnessHost(
        host_id="gpu-pc",
        display_name="GPU PC",
        kind="linux",
        status="online",
        connection_kind="direct",
        last_seen_at=now - timedelta(seconds=120),
    )
    current_host = [fresh_host]

    class _Registry:
        def list_hosts(self):
            return [current_host[0]]

    refreshes = []
    monotonic_clock = [0.0]
    payload = {
        **GOOD_PAYLOAD,
        "accounts": [
            {
                **GOOD_PAYLOAD["accounts"][0],
                "host_id": "gpu-pc",
                "observed_at": now.isoformat(),
                "windows": [
                    {
                        "window_kind": "primary",
                        "window_label": "5 hours",
                        "reset_at": (now + timedelta(hours=5)).isoformat(),
                        "used_percent": 12.5,
                        "used_tokens": 125,
                        "limit_tokens": 1000,
                    }
                ],
            }
        ],
    }
    loop = ProviderRefreshLoop(
        provider_usage=service,
        registry=_Registry(),
        shutdown_event=threading.Event(),
        interval_seconds=300,
        clock=lambda: monotonic_clock[0],
        fetch=lambda item: refreshes.append(item.host_id) or payload,
    )

    # Initial run when host is fresh - probes and succeeds
    loop.run_once()
    assert refreshes == ["gpu-pc"]
    assert service.latest_accounts()[0].status == "ok"

    # Host becomes stale - refresh loop skips probe and marks offline
    current_host[0] = stale_host
    monotonic_clock[0] = 10.0
    loop.run_once()
    assert refreshes == ["gpu-pc"]  # No second probe!
    accounts = service.latest_accounts()
    assert len(accounts) == 1
    assert accounts[0].status == "stale"
    assert accounts[0].error_category == "host_offline"

    # Host heartbeats again -- probing has to resume on its own. Nothing in the
    # hub clears the skip, so if the fresh last_seen_at did not re-enable the
    # probe the host would stay dark forever with no error anywhere to show it.
    current_host[0] = HarnessHost(
        host_id="gpu-pc",
        display_name="GPU PC",
        kind="linux",
        status="online",
        connection_kind="direct",
        last_seen_at=datetime.now(timezone.utc),
    )
    monotonic_clock[0] = 20.0
    loop.run_once()
    assert refreshes == ["gpu-pc", "gpu-pc"]
    recovered = service.latest_accounts()
    assert len(recovered) == 1
    assert recovered[0].status == "ok"
    assert recovered[0].error_category is None


def test_harness_host_is_stale_behavior():
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    fresh_time = now - timedelta(seconds=10)
    old_time = now - timedelta(seconds=120)

    fresh_host = HarnessHost(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        status="online",
        connection_kind="direct",
        last_seen_at=fresh_time,
    )
    stale_host = HarnessHost(
        host_id="gpu-pc",
        display_name="GPU PC",
        kind="linux",
        status="online",
        connection_kind="direct",
        last_seen_at=old_time,
    )
    relay_host = HarnessHost(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        status="online",
        connection_kind="relay",
        last_seen_at=old_time,
    )

    assert fresh_host.is_stale(now=now) is False
    assert stale_host.is_stale(now=now) is True
    # Relay hosts don't rely on last_seen_at for staleness
    assert relay_host.is_stale(now=now) is False


def test_is_stale_handles_naive_db_timestamps_against_an_aware_now():
    """A naive last_seen_at is process-local, so an aware `now` must convert to local.

    DuckDB stores last_seen_at as TIMESTAMP, never TIMESTAMPTZ, so it reads back
    naive *local* (see registry._db_timestamp_to_utc). Normalizing an aware `now`
    to naive UTC instead shifts the comparison by the hub's UTC offset: west of
    UTC every direct host reports stale seconds after heartbeating, which takes
    all provider capacity dark; east of UTC nothing is ever stale. The other
    is_stale tests only pass aware/aware, which cannot catch either direction.
    """

    def _host(last_seen):
        return HarnessHost(
            host_id="gpu-pc",
            display_name="GPU PC",
            kind="linux",
            status="online",
            connection_kind="direct",
            last_seen_at=last_seen,
        )

    local_now = datetime.now()
    aware_now = datetime.now(timezone.utc)

    just_heartbeat = _host(local_now - timedelta(seconds=5))
    long_gone = _host(local_now - timedelta(seconds=600))

    # The default (now=None) path is the one production takes today.
    assert just_heartbeat.is_stale() is False
    assert long_gone.is_stale() is True

    # An explicit aware `now` has to agree with it, whatever the hub's offset.
    assert just_heartbeat.is_stale(now=aware_now) is False
    assert long_gone.is_stale(now=aware_now) is True


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
