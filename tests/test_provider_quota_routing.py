"""Routing uses independent subscriptions and only confirmed, current windows."""

from datetime import datetime, timedelta, timezone

import pytest

from drover.server.mcp.tools import _build_routing_hint, drover_provider_quota
from drover.server.providers.types import ProviderAccountSnapshot, ProviderUsageWindow

NOW = datetime(2026, 10, 12, 20, tzinfo=timezone.utc)


def window(kind="primary", used=2, status="ok", reset_hours=2):
    return {
        "kind": kind,
        "used_percent": used,
        "status": status,
        "resets_at": (
            (NOW + timedelta(hours=reset_hours)).isoformat()
            if reset_hours is not None
            else None
        ),
    }


def account(*windows, provider="openai", status="ok"):
    return {"provider": provider, "status": status, "windows": list(windows)}


def test_best_account_uses_its_own_binding_window():
    accounts = [
        account(window(used=100), window("secondary", used=0)),
        account(window(used=0), window("secondary", used=2)),
    ]
    assert _build_routing_hint(accounts, NOW) == "codex: 2%"
    assert _build_routing_hint(list(reversed(accounts)), NOW) == "codex: 2%"


def test_single_exhausted_constraint_blocks_account():
    assert (
        _build_routing_hint(
            [account(window(used=100), window("secondary", used=2))], NOW
        )
        == "codex: out until 22:00Z"
    )


def test_recovery_waits_for_all_constraints_then_chooses_first_account():
    accounts = [
        account(
            window(used=100, reset_hours=1), window("secondary", 100, reset_hours=6)
        ),
        account(window(used=100, reset_hours=3)),
    ]
    assert (
        _build_routing_hint(accounts[:1], NOW) == "codex: out until 2026-10-13 02:00Z"
    )
    assert _build_routing_hint(accounts, NOW) == "codex: out until 23:00Z"


@pytest.mark.parametrize("reset_hours", [None, 2])
def test_observed_exhaustion_does_not_hide_alternative(reset_hours):
    blocked = account(
        window(used=100, status="observed_exhausted", reset_hours=reset_hours),
        status="observed_exhausted",
    )
    assert _build_routing_hint([blocked, account(window())], NOW) == "codex: 2%"
    assert "observed exhausted" in _build_routing_hint([blocked], NOW)


@pytest.mark.parametrize("status", ["stale", "error", "usage_unavailable"])
def test_unusable_accounts_never_supply_percentages(status):
    hint = _build_routing_hint([account(window(used=9), status=status)], NOW)
    assert "%" not in hint
    assert "no provider accounts" not in hint
    assert (
        _build_routing_hint(
            [account(window(used=0), status=status), account(window(used=32))], NOW
        )
        == "codex: 32%"
    )


@pytest.mark.parametrize("reset_hours", [-1, 0])
def test_expired_observation_is_stale(reset_hours):
    hint = _build_routing_hint(
        [
            account(
                window(used=100, status="observed_exhausted", reset_hours=reset_hours),
                status="observed_exhausted",
            )
        ],
        NOW,
    )
    assert hint == "codex: stale"


def test_google_without_windows_remains_configured():
    assert _build_routing_hint([account(provider="google")], NOW) == (
        "gemini: configured, usage unknown; agy-claude: configured, usage unknown"
    )


def test_not_configured_hosts_do_not_count_as_fleet_quota_accounts(tmp_path):
    snapshot = ProviderAccountSnapshot(
        snapshot_id="claude-not-configured",
        dedup_key="claude-not-configured",
        provider="anthropic",
        account_label="Claude Code",
        plan_label=None,
        host_id="nas",
        status="not_configured",
        observed_at=NOW,
        windows=(),
        source="claude-oauth-usage",
        error_category="not_configured",
    )

    class Service:
        def latest_accounts(self):
            return [snapshot]

    result = drover_provider_quota(
        duckdb_path=tmp_path / "quota.duckdb", service=Service(), now=NOW
    )

    assert result["accounts"] == []
    assert result["routing_hint"] == "no provider accounts configured"


@pytest.mark.parametrize("used", [0, 99, 100])
def test_unknown_kinds_cannot_establish_headroom_or_exhaustion(used):
    assert (
        _build_routing_hint(
            [
                account(
                    window("unsupported", used),
                    window("five_hour", 2),
                    provider="anthropic",
                )
            ],
            NOW,
        )
        == "claude: configured, usage unknown"
    )
    hint = _build_routing_hint(
        [account(window("unsupported_3p", used), provider="google")], NOW
    )
    assert (
        hint
        == "gemini: configured, usage unknown; agy-claude: configured, usage unknown"
    )


def test_partial_unknown_usage_does_not_establish_headroom():
    assert (
        _build_routing_hint(
            [account(window(), window("secondary", None, "usage_unavailable"))], NOW
        )
        == "codex: configured, usage unknown"
    )


def test_mixed_providers_and_google_pools_remain_independent():
    accounts = [
        account(window("five_hour", 32), provider="anthropic"),
        account(window(used=100)),
        account(window(used=2)),
        account(window("five_hour", 0), provider="google", status="stale"),
        account(
            window("five_hour", 30),
            window("seven_day", 10),
            window("seven_day_claude_gpt", 100, "observed_exhausted"),
            provider="google",
            status="observed_exhausted",
        ),
    ]
    assert _build_routing_hint(accounts, NOW) == (
        "gemini: 30%; codex: 2%; agy-claude: observed exhausted until 22:00Z; claude: 32%"
    )


@pytest.mark.parametrize(
    "status,source,reset_hours,expected",
    [
        ("stale", "codex-app-server", 2, "stale"),
        ("usage_unavailable", "codex-app-server", 2, "usage_unavailable"),
        ("observed_exhausted", "observed_429", -1, "stale"),
    ],
)
def test_projected_windows_preserve_unusable_state(
    tmp_path, status, source, reset_hours, expected
):
    snapshot = ProviderAccountSnapshot(
        snapshot_id="synthetic",
        dedup_key="synthetic",
        provider="openai",
        account_label="Test subscription",
        plan_label=None,
        host_id="test-host",
        status=status,
        observed_at=NOW,
        windows=(
            ProviderUsageWindow(
                kind="primary",
                used_percent=100,
                resets_at=NOW + timedelta(hours=reset_hours),
            ),
        ),
        source=source,
    )

    class Service:
        def latest_accounts(self):
            return [snapshot]

    result = drover_provider_quota(
        duckdb_path=tmp_path / "quota.duckdb", service=Service(), now=NOW
    )
    assert result["accounts"][0]["windows"][0]["status"] == expected
    assert "%" not in result["routing_hint"]
    assert "observed exhausted" not in result["routing_hint"]


def test_quota_projects_independent_accounts_and_unknown_google(tmp_path):
    def snapshot(label, used):
        return ProviderAccountSnapshot(
            snapshot_id=label,
            dedup_key=label,
            provider="openai",
            account_label=label,
            plan_label=None,
            host_id="test-host",
            status="ok",
            observed_at=NOW,
            windows=(
                ProviderUsageWindow(
                    kind="primary",
                    used_percent=used,
                    resets_at=NOW + timedelta(hours=2),
                ),
            ),
            source="codex-app-server",
        )

    class Service:
        def latest_accounts(self):
            return [
                snapshot("Subscription A", 100),
                snapshot("Subscription B", 2),
                ProviderAccountSnapshot(
                    snapshot_id="google",
                    dedup_key="google",
                    provider="google",
                    account_label="Google subscription",
                    plan_label=None,
                    host_id="test-host",
                    status="usage_unavailable",
                    observed_at=NOW,
                    windows=(),
                    source="agy-usage",
                ),
            ]

    result = drover_provider_quota(
        duckdb_path=tmp_path / "quota.duckdb", service=Service(), now=NOW
    )
    assert len(result["accounts"]) == 3
    assert result["routing_hint"] == (
        "gemini: configured, usage unknown; codex: 2%; agy-claude: configured, usage unknown"
    )


@pytest.mark.parametrize(
    "old_status,reset_hours", [("stale", 2), ("observed_exhausted", -1)]
)
def test_current_host_reading_beats_stale_or_expired_duplicate(
    tmp_path, old_status, reset_hours
):
    def snapshot(host, status, used, reset_hours):
        return ProviderAccountSnapshot(
            snapshot_id=host,
            dedup_key=host,
            provider="openai",
            account_label="Shared test subscription",
            plan_label=None,
            host_id=host,
            status=status,
            observed_at=NOW,
            windows=(
                ProviderUsageWindow(
                    kind="primary",
                    used_percent=used,
                    resets_at=NOW + timedelta(hours=reset_hours),
                ),
            ),
            source=(
                "observed_429" if status == "observed_exhausted" else "codex-app-server"
            ),
        )

    class Service:
        def latest_accounts(self):
            return [
                snapshot("old-host", old_status, 100, reset_hours),
                snapshot("current-host", "ok", 2, 2),
            ]

    result = drover_provider_quota(
        duckdb_path=tmp_path / "quota.duckdb", service=Service(), now=NOW
    )
    assert len(result["accounts"]) == 1
    assert result["accounts"][0]["hosts"] == ["current-host", "old-host"]
    assert result["routing_hint"] == "codex: 2%"
