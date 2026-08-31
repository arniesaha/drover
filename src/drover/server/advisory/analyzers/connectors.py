"""Deterministic provider connector health analyzers."""

from __future__ import annotations

from datetime import datetime, timedelta

from drover.server.advisory.analyzers import (
    AnalysisSnapshot,
    ProviderConnectionObservation,
)
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    FindingCandidate,
    FindingEvidence,
    Severity,
)


def _age_seconds(now: datetime, then: datetime | None) -> int | None:
    if then is None:
        return None
    return max(0, int((now - then).total_seconds()))


def _target(connection: ProviderConnectionObservation) -> str:
    return f"{connection.host_id}/{connection.provider}/{connection.account_label}"


class ConnectorFreshnessAnalyzer:
    analyzer_id = "deterministic.connector_freshness"

    def __init__(
        self,
        *,
        max_age: timedelta = timedelta(hours=6),
        error_grace: timedelta = timedelta(minutes=30),
        host_absence: timedelta = timedelta(minutes=10),
    ) -> None:
        for name, value in (
            ("max_age", max_age),
            ("error_grace", error_grace),
            ("host_absence", host_absence),
        ):
            if value <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        self.max_age = max_age
        self.error_grace = error_grace
        self.host_absence = host_absence

    def analyze(self, snapshot: AnalysisSnapshot) -> list[FindingCandidate]:
        findings: list[FindingCandidate] = []
        for connection in sorted(
            snapshot.provider_connections,
            key=lambda item: (item.host_id, item.provider, item.account_label),
        ):
            if not connection.enabled:
                continue
            host_age = _age_seconds(snapshot.analyzed_at, connection.host_last_seen_at)
            if host_age is None or host_age > self.host_absence.total_seconds():
                continue
            success_age = _age_seconds(snapshot.analyzed_at, connection.last_success_at)
            error_is_persistent = success_age is None or (
                success_age > self.error_grace.total_seconds()
            )
            if (
                connection.status == "error" or connection.error_category
            ) and error_is_persistent:
                category = connection.error_category or "unknown"
                findings.append(
                    FindingCandidate(
                        analyzer_id=self.analyzer_id,
                        rule_id="connector.error",
                        target_type="provider_connector",
                        target_id=_target(connection),
                        analyzer_class=AnalyzerClass.DETERMINISTIC,
                        severity=Severity.HIGH,
                        confidence=Confidence.CONFIRMED,
                        title=f"{connection.provider} connector reports an error",
                        impact="Provider-reported subscription usage may be stale or unavailable until the connector succeeds.",
                        remediation=(
                            f"Review the {category} error for the {connection.provider} connector for account {connection.account_label} on host {connection.host_id}, correct it outside Drover, refresh the connector, then run Check Again.",
                        ),
                        evidence=(
                            FindingEvidence(
                                source_ref=connection.source_ref,
                                observed_at=connection.observed_at,
                                fields={
                                    "error_category": category,
                                    "attempt_age_seconds": _age_seconds(
                                        snapshot.analyzed_at,
                                        connection.last_attempt_at,
                                    ),
                                    "last_success_age_seconds": _age_seconds(
                                        snapshot.analyzed_at,
                                        connection.last_success_at,
                                    ),
                                    "enabled": connection.enabled,
                                    "status": connection.status,
                                    "host_last_seen_age_seconds": host_age,
                                },
                            ),
                        ),
                    )
                )
            freshness_time = connection.last_success_at or connection.observed_at
            age_seconds = _age_seconds(snapshot.analyzed_at, freshness_time)
            maximum_age_seconds = int(self.max_age.total_seconds())
            if connection.status == "stale" or (
                age_seconds is not None and age_seconds > maximum_age_seconds
            ):
                findings.append(
                    FindingCandidate(
                        analyzer_id=self.analyzer_id,
                        rule_id="connector.stale",
                        target_type="provider_connector",
                        target_id=_target(connection),
                        analyzer_class=AnalyzerClass.DETERMINISTIC,
                        severity=Severity.HIGH,
                        confidence=Confidence.CONFIRMED,
                        title=f"{connection.provider} connector data is stale",
                        impact="Provider capacity and reset information may no longer describe the active subscription window.",
                        remediation=(
                            f"Refresh the {connection.provider} connector for account {connection.account_label} on host {connection.host_id}, then run Check Again.",
                        ),
                        evidence=(
                            FindingEvidence(
                                source_ref=connection.source_ref,
                                observed_at=connection.observed_at,
                                fields={
                                    "age_seconds": age_seconds,
                                    "maximum_age_seconds": maximum_age_seconds,
                                    "enabled": connection.enabled,
                                    "status": connection.status,
                                    "host_last_seen_age_seconds": host_age,
                                },
                            ),
                        ),
                    )
                )
        return sorted(findings, key=lambda item: (item.target_id, item.rule_id))


class ProviderResetWindowAnalyzer:
    analyzer_id = "deterministic.provider_reset_windows"

    def analyze(self, snapshot: AnalysisSnapshot) -> list[FindingCandidate]:
        findings: list[FindingCandidate] = []
        for connection in sorted(
            snapshot.provider_connections,
            key=lambda item: (item.host_id, item.provider, item.account_label),
        ):
            if not connection.enabled:
                continue
            if not connection.reset_windows_complete:
                continue
            invalid = [
                window
                for window in connection.reset_windows
                if window.starts_at is not None
                and window.resets_at is not None
                and window.resets_at <= window.starts_at
            ]
            if not invalid:
                continue
            durations = [
                int((window.resets_at - window.starts_at).total_seconds())
                for window in invalid
                if window.resets_at is not None and window.starts_at is not None
            ]
            kinds = ", ".join(sorted({window.kind for window in invalid}))
            findings.append(
                FindingCandidate(
                    analyzer_id=self.analyzer_id,
                    rule_id="connector.contradictory_reset_window",
                    target_type="provider_connector",
                    target_id=_target(connection),
                    analyzer_class=AnalyzerClass.DETERMINISTIC,
                    severity=Severity.HIGH,
                    confidence=Confidence.CONFIRMED,
                    title=f"{connection.provider} reports a contradictory reset window",
                    impact="A reset countdown cannot be trusted when its reset time does not follow its start time.",
                    remediation=(
                        f"Refresh the {connection.provider} connector for account {connection.account_label} on host {connection.host_id} and verify its {kinds} reset window before relying on the countdown.",
                    ),
                    evidence=(
                        FindingEvidence(
                            source_ref=connection.source_ref,
                            observed_at=connection.observed_at,
                            fields={
                                "invalid_window_count": len(invalid),
                                "total_window_count": len(connection.reset_windows),
                                "minimum_window_duration_seconds": min(durations),
                            },
                        ),
                    ),
                )
            )
        return findings
