"""Deterministic operational advisory analyzer contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from drover.server.advisory.analyzers import (
    AnalysisSnapshot,
    HookDescriptor,
    ProviderConnectionObservation,
    ProviderResetWindow,
    RoutingAggregate,
    TelemetryAggregate,
)
from drover.server.advisory.analyzers.connectors import (
    ConnectorFreshnessAnalyzer,
    ProviderResetWindowAnalyzer,
)
from drover.server.advisory.analyzers.hooks import HookValidityAnalyzer
from drover.server.advisory.analyzers.routing import RoutingMismatchAnalyzer
from drover.server.advisory.analyzers.telemetry import (
    CacheReadEfficiencyAnalyzer,
    TelemetryCoverageAnalyzer,
)
from drover.server.advisory.types import Confidence, Severity

NOW = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)


def _snapshot(
    *,
    providers: tuple[ProviderConnectionObservation, ...] = (),
    telemetry: tuple[TelemetryAggregate, ...] = (),
    routing: tuple[RoutingAggregate, ...] = (),
    hooks: tuple[HookDescriptor, ...] = (),
) -> AnalysisSnapshot:
    return AnalysisSnapshot(
        source_version="lakehouse:v7",
        analyzed_at=NOW,
        provider_connections=providers,
        telemetry=telemetry,
        routing=routing,
        hooks=hooks,
    )


def _provider(**overrides: object) -> ProviderConnectionObservation:
    values: dict[str, object] = {
        "provider": "openai",
        "account_label": "personal",
        "host_id": "mac-mini",
        "enabled": True,
        "status": "ok",
        "observed_at": NOW - timedelta(minutes=20),
        "last_attempt_at": NOW - timedelta(minutes=20),
        "last_success_at": NOW - timedelta(minutes=20),
        "error_category": None,
        "reset_windows": (),
        "source_ref": "provider_connections:openai/personal/mac-mini",
    }
    values.update(overrides)
    return ProviderConnectionObservation(**values)  # type: ignore[arg-type]


def _telemetry(**overrides: object) -> TelemetryAggregate:
    values: dict[str, object] = {
        "target_id": "mac-mini/codex",
        "host_id": "mac-mini",
        "harness_id": "codex",
        "observed_at": NOW,
        "total_sessions": 10,
        "sessions_with_spans": 10,
        "repository_attributed_sessions": 10,
        "token_observed_sessions": 10,
        "cost_observed_sessions": 10,
        "prompt_tokens": 10_000,
        "cache_read_tokens": 5_000,
        "source_ref": "analytics:mac-mini/codex/24h",
    }
    values.update(overrides)
    return TelemetryAggregate(**values)  # type: ignore[arg-type]


def test_analysis_snapshot_is_frozen_and_bounded() -> None:
    snapshot = _snapshot()

    with pytest.raises(FrozenInstanceError):
        snapshot.source_version = "changed"  # type: ignore[misc]

    with pytest.raises(ValueError, match="telemetry is bounded"):
        _snapshot(
            telemetry=tuple(_telemetry(target_id=str(index)) for index in range(513))
        )


def test_stale_connector_is_confirmed_high_with_exact_age() -> None:
    snapshot = _snapshot(providers=(_provider(),))

    finding = ConnectorFreshnessAnalyzer(max_age=timedelta(minutes=15)).analyze(
        snapshot
    )[0]

    assert (finding.rule_id, finding.severity, finding.confidence) == (
        "connector.stale",
        Severity.HIGH,
        Confidence.CONFIRMED,
    )
    assert finding.evidence[0].fields == {
        "age_seconds": 1200,
        "maximum_age_seconds": 900,
        "enabled": True,
        "status": "ok",
    }
    assert finding.remediation == (
        "Refresh the openai connector for account personal on host mac-mini, then run Check Again.",
    )


def test_connector_error_reports_classification_and_attempt_age() -> None:
    snapshot = _snapshot(
        providers=(
            _provider(
                status="error",
                error_category="authentication",
                observed_at=NOW,
                last_attempt_at=NOW - timedelta(seconds=30),
                last_success_at=NOW - timedelta(minutes=2),
            ),
        )
    )

    findings = ConnectorFreshnessAnalyzer(max_age=timedelta(minutes=15)).analyze(
        snapshot
    )
    error = next(item for item in findings if item.rule_id == "connector.error")

    assert error.evidence[0].fields == {
        "error_category": "authentication",
        "attempt_age_seconds": 30,
        "last_success_age_seconds": 120,
        "enabled": True,
        "status": "error",
    }
    assert "Review the authentication error" in error.remediation[0]


def test_provider_stale_status_is_a_finding_even_before_age_threshold() -> None:
    snapshot = _snapshot(
        providers=(
            _provider(
                status="stale",
                observed_at=NOW,
                last_attempt_at=NOW,
                last_success_at=NOW,
            ),
        )
    )

    findings = ConnectorFreshnessAnalyzer(max_age=timedelta(minutes=15)).analyze(
        snapshot
    )

    assert [item.rule_id for item in findings] == ["connector.stale"]
    assert findings[0].evidence[0].fields["age_seconds"] == 0


def test_contradictory_provider_reset_window_is_confirmed() -> None:
    bad_window = ProviderResetWindow(
        kind="five_hour",
        starts_at=NOW + timedelta(hours=2),
        resets_at=NOW + timedelta(hours=1),
    )
    snapshot = _snapshot(
        providers=(
            _provider(
                observed_at=NOW,
                last_attempt_at=NOW,
                last_success_at=NOW,
                reset_windows=(bad_window,),
            ),
        )
    )

    finding = ProviderResetWindowAnalyzer().analyze(snapshot)[0]

    assert (finding.rule_id, finding.confidence) == (
        "connector.contradictory_reset_window",
        Confidence.CONFIRMED,
    )
    assert finding.evidence[0].fields == {
        "invalid_window_count": 1,
        "total_window_count": 1,
        "minimum_window_duration_seconds": -3600,
    }
    assert finding.remediation == (
        "Refresh the openai connector for account personal on host mac-mini and verify its five_hour reset window before relying on the countdown.",
    )


def test_missing_hook_executable_is_confirmed_without_opening_hook_files() -> None:
    hook = HookDescriptor(
        hook_id="claude.stop",
        host_id="mac-mini",
        harness_id="claude",
        canonical_config_path="/Users/operator/.claude/settings.json",
        canonical_executable_path="/opt/drover/bin/drover-hook",
        enabled=True,
        executable_exists=False,
        executable_is_file=False,
        executable_is_executable=False,
        allowlisted=True,
        observed_at=NOW,
        source_ref="harness-inventory:mac-mini/claude/hooks/stop",
    )

    finding = HookValidityAnalyzer().analyze(_snapshot(hooks=(hook,)))[0]

    assert (finding.rule_id, finding.confidence) == (
        "hook.missing_executable",
        Confidence.CONFIRMED,
    )
    assert finding.evidence[0].fields == {
        "enabled": True,
        "executable_exists": False,
        "executable_is_file": False,
        "executable_is_executable": False,
    }
    assert finding.remediation[0].startswith("Restore executable")


def test_hook_descriptor_rejects_non_allowlisted_or_noncanonical_input() -> None:
    fields = {
        "hook_id": "claude.stop",
        "host_id": "mac-mini",
        "harness_id": "claude",
        "canonical_config_path": "/Users/operator/.claude/settings.json",
        "canonical_executable_path": "/opt/drover/bin/drover-hook",
        "enabled": True,
        "executable_exists": True,
        "executable_is_file": True,
        "executable_is_executable": True,
        "allowlisted": True,
        "observed_at": NOW,
        "source_ref": "harness-inventory:mac-mini/claude/hooks/stop",
    }

    with pytest.raises(ValueError, match="allowlisted"):
        HookDescriptor(**(fields | {"allowlisted": False}))
    with pytest.raises(ValueError, match="canonical_executable_path"):
        HookDescriptor(**(fields | {"canonical_executable_path": "bin/drover-hook"}))
    with pytest.raises(ValueError, match="canonical_executable_path"):
        HookDescriptor(
            **(fields | {"canonical_executable_path": "/opt/drover/../bin/drover-hook"})
        )


@pytest.mark.parametrize(
    ("field", "rule_id", "coverage_field"),
    [
        ("sessions_with_spans", "telemetry.flow_coverage", "span_coverage_percent"),
        (
            "repository_attributed_sessions",
            "telemetry.repository_attribution",
            "coverage_percent",
        ),
        ("token_observed_sessions", "telemetry.token_coverage", "coverage_percent"),
        ("cost_observed_sessions", "telemetry.cost_coverage", "coverage_percent"),
    ],
)
def test_telemetry_coverage_rules_report_aggregate_percentages(
    field: str, rule_id: str, coverage_field: str
) -> None:
    aggregate = _telemetry(**{field: 4})

    findings = TelemetryCoverageAnalyzer(minimum_percent=80).analyze(
        _snapshot(telemetry=(aggregate,))
    )
    finding = next(item for item in findings if item.rule_id == rule_id)

    assert finding.evidence[0].fields[coverage_field] == 40
    assert finding.evidence[0].fields["covered_sessions"] == 4
    assert finding.evidence[0].fields["total_sessions"] == 10
    assert finding.evidence[0].fields["minimum_percent"] == 80


def test_empty_telemetry_window_does_not_create_coverage_findings() -> None:
    aggregate = _telemetry(
        total_sessions=0,
        sessions_with_spans=0,
        repository_attributed_sessions=0,
        token_observed_sessions=0,
        cost_observed_sessions=0,
        prompt_tokens=0,
        cache_read_tokens=0,
    )

    assert TelemetryCoverageAnalyzer().analyze(_snapshot(telemetry=(aggregate,))) == []


def test_low_cache_read_ratio_reports_numerical_evidence() -> None:
    aggregate = _telemetry(prompt_tokens=9_500, cache_read_tokens=500)

    finding = CacheReadEfficiencyAnalyzer(
        minimum_input_tokens=1_000,
        minimum_cache_read_percent=20,
    ).analyze(_snapshot(telemetry=(aggregate,)))[0]

    assert finding.rule_id == "telemetry.cache_read_inefficiency"
    assert finding.evidence[0].fields == {
        "prompt_tokens": 9500,
        "cache_read_tokens": 500,
        "reusable_input_tokens": 10000,
        "cache_read_percent": 5,
        "minimum_cache_read_percent": 20,
    }
    assert "Inspect repeated context" in finding.remediation[0]


def test_routing_mismatch_frequency_is_confirmed() -> None:
    routing = RoutingAggregate(
        target_id="mac-mini/codex/openai",
        host_id="mac-mini",
        harness_id="codex",
        provider="openai",
        observed_at=NOW,
        decision_count=20,
        mismatch_count=5,
        source_ref="routing:mac-mini/codex/openai/24h",
    )

    finding = RoutingMismatchAnalyzer(
        minimum_decisions=10, maximum_mismatch_percent=10
    ).analyze(_snapshot(routing=(routing,)))[0]

    assert (finding.rule_id, finding.confidence) == (
        "routing.mismatch_frequency",
        Confidence.CONFIRMED,
    )
    assert finding.evidence[0].fields == {
        "decision_count": 20,
        "mismatch_count": 5,
        "mismatch_percent": 25,
        "maximum_mismatch_percent": 10,
    }
    assert finding.remediation == (
        "Review routing policy and fallback events for provider openai on codex at host mac-mini, correct the declared route outside Drover if needed, then run Check Again.",
    )


def test_analyzers_return_candidates_in_stable_target_order() -> None:
    later = _telemetry(
        target_id="z-host/codex", host_id="z-host", token_observed_sessions=1
    )
    earlier = _telemetry(
        target_id="a-host/codex", host_id="a-host", token_observed_sessions=1
    )

    findings = TelemetryCoverageAnalyzer(minimum_percent=80).analyze(
        _snapshot(telemetry=(later, earlier))
    )

    token_targets = [
        item.target_id
        for item in findings
        if item.rule_id == "telemetry.token_coverage"
    ]
    assert token_targets == ["a-host/codex", "z-host/codex"]
