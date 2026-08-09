"""Deterministic coverage and cache-efficiency analyzers."""

from __future__ import annotations

from dataclasses import dataclass

from drover.server.advisory.analyzers import AnalysisSnapshot, TelemetryAggregate
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    FindingCandidate,
    FindingEvidence,
    Severity,
)


def _percent(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100, 2)


@dataclass(frozen=True)
class _CoverageRule:
    count_field: str
    rule_id: str
    percentage_field: str
    title: str
    impact: str
    remediation: str
    severity: Severity


_COVERAGE_RULES = (
    _CoverageRule(
        count_field="sessions_with_spans",
        rule_id="telemetry.flow_coverage",
        percentage_field="span_coverage_percent",
        title="Telemetry flow is incomplete",
        impact="Observed latency, routing, token, cost, and cache analytics omit sessions without spans.",
        remediation="Verify the OTLP exporter and collector path for {target}, restore span delivery outside Drover, then run Check Again.",
        severity=Severity.HIGH,
    ),
    _CoverageRule(
        count_field="repository_attributed_sessions",
        rule_id="telemetry.repository_attribution",
        percentage_field="coverage_percent",
        title="Repository attribution coverage is low",
        impact="Project rankings and repository drilldowns omit unattributed sessions.",
        remediation="Verify repository identity metadata for {target}, correct the emitting harness configuration outside Drover, then run Check Again.",
        severity=Severity.MEDIUM,
    ),
    _CoverageRule(
        count_field="token_observed_sessions",
        rule_id="telemetry.token_coverage",
        percentage_field="coverage_percent",
        title="Token coverage is low",
        impact="Token totals and token-ranked projects do not represent enough observed sessions.",
        remediation="Verify token attributes from the model instrumentation for {target}, correct the exporter outside Drover, then run Check Again.",
        severity=Severity.MEDIUM,
    ),
    _CoverageRule(
        count_field="cost_observed_sessions",
        rule_id="telemetry.cost_coverage",
        percentage_field="coverage_percent",
        title="Cost coverage is low",
        impact="Observed API cost totals omit sessions that do not emit a cost field.",
        remediation="Verify cost attributes from the model instrumentation for {target}, correct the exporter outside Drover, then run Check Again.",
        severity=Severity.MEDIUM,
    ),
)


class TelemetryCoverageAnalyzer:
    analyzer_id = "deterministic.telemetry_coverage"

    def __init__(self, *, minimum_percent: float = 80) -> None:
        if not 0 < minimum_percent <= 100:
            raise ValueError("minimum_percent must be within (0, 100]")
        self.minimum_percent = minimum_percent

    def analyze(self, snapshot: AnalysisSnapshot) -> list[FindingCandidate]:
        findings: list[FindingCandidate] = []
        for aggregate in sorted(snapshot.telemetry, key=lambda item: item.target_id):
            if aggregate.total_sessions == 0:
                continue
            for rule in _COVERAGE_RULES:
                covered = getattr(aggregate, rule.count_field)
                coverage = _percent(covered, aggregate.total_sessions)
                if coverage >= self.minimum_percent:
                    continue
                findings.append(
                    FindingCandidate(
                        analyzer_id=self.analyzer_id,
                        rule_id=rule.rule_id,
                        target_type="telemetry_source",
                        target_id=aggregate.target_id,
                        analyzer_class=AnalyzerClass.DETERMINISTIC,
                        severity=rule.severity,
                        confidence=Confidence.CONFIRMED,
                        title=rule.title,
                        impact=rule.impact,
                        remediation=(
                            rule.remediation.format(target=aggregate.target_id),
                        ),
                        evidence=(
                            FindingEvidence(
                                source_ref=aggregate.source_ref,
                                observed_at=aggregate.observed_at,
                                fields={
                                    rule.percentage_field: coverage,
                                    "covered_sessions": covered,
                                    "total_sessions": aggregate.total_sessions,
                                    "minimum_percent": self.minimum_percent,
                                },
                            ),
                        ),
                    )
                )
        return findings


class CacheReadEfficiencyAnalyzer:
    analyzer_id = "deterministic.cache_read_efficiency"

    def __init__(
        self,
        *,
        minimum_input_tokens: int = 10_000,
        minimum_cache_read_percent: float = 10,
    ) -> None:
        if minimum_input_tokens <= 0:
            raise ValueError("minimum_input_tokens must be positive")
        if not 0 <= minimum_cache_read_percent <= 100:
            raise ValueError("minimum_cache_read_percent must be within [0, 100]")
        self.minimum_input_tokens = minimum_input_tokens
        self.minimum_cache_read_percent = minimum_cache_read_percent

    def analyze(self, snapshot: AnalysisSnapshot) -> list[FindingCandidate]:
        findings: list[FindingCandidate] = []
        for aggregate in sorted(snapshot.telemetry, key=lambda item: item.target_id):
            reusable_input = aggregate.prompt_tokens + aggregate.cache_read_tokens
            if reusable_input < self.minimum_input_tokens or reusable_input == 0:
                continue
            cache_percent = _percent(aggregate.cache_read_tokens, reusable_input)
            if cache_percent >= self.minimum_cache_read_percent:
                continue
            findings.append(
                FindingCandidate(
                    analyzer_id=self.analyzer_id,
                    rule_id="telemetry.cache_read_inefficiency",
                    target_type="telemetry_source",
                    target_id=aggregate.target_id,
                    analyzer_class=AnalyzerClass.DETERMINISTIC,
                    severity=Severity.MEDIUM,
                    confidence=Confidence.CONFIRMED,
                    title="Cache-read efficiency is low",
                    impact="Repeated input context is consuming uncached model tokens and may increase latency or API cost.",
                    remediation=(
                        f"Inspect repeated context for {aggregate.target_id}, adjust cache-compatible prompt assembly outside Drover, then run Check Again.",
                    ),
                    evidence=(
                        FindingEvidence(
                            source_ref=aggregate.source_ref,
                            observed_at=aggregate.observed_at,
                            fields={
                                "prompt_tokens": aggregate.prompt_tokens,
                                "cache_read_tokens": aggregate.cache_read_tokens,
                                "reusable_input_tokens": reusable_input,
                                "cache_read_percent": cache_percent,
                                "minimum_cache_read_percent": self.minimum_cache_read_percent,
                            },
                        ),
                    ),
                )
            )
        return findings
