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
from drover.server.harness.usage import CACHE_INSIDE_INPUT_HARNESSES


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
        count_field="repository_attributed_sessions",
        rule_id="telemetry.repository_attribution",
        percentage_field="coverage_percent",
        title="Repository attribution coverage is low",
        impact="Project rankings and repository drilldowns omit unattributed sessions.",
        remediation="Verify repository identity metadata for {target}, correct the emitting harness configuration outside Drover, then run Check Again.",
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
        active = [
            a for a in snapshot.telemetry if a.facts_complete and a.total_sessions > 0
        ]
        if not active:
            return findings

        if sum(a.sessions_with_spans for a in active) == 0:
            latest = max(
                (a.latest_span_at for a in active if a.latest_span_at),
                default=None,
            )
            findings.append(
                FindingCandidate(
                    analyzer_id=self.analyzer_id,
                    rule_id="telemetry.span_feed_silent",
                    target_type="telemetry_source",
                    target_id="fleet",
                    analyzer_class=AnalyzerClass.DETERMINISTIC,
                    severity=Severity.LOW,
                    confidence=Confidence.CONFIRMED,
                    title="Span feed is silent",
                    impact="Observed cost, latency, and routing enrichment are unavailable; token analytics fall back to harness-reported usage.",
                    remediation=(
                        "Configure an OTLP span producer (for example the "
                        "tempo_relay section in collect.toml) or accept "
                        "span-derived metrics as unavailable.",
                    ),
                    evidence=(
                        FindingEvidence(
                            source_ref="analytics:fleet",
                            observed_at=snapshot.analyzed_at,
                            fields={
                                "total_sessions": sum(a.total_sessions for a in active),
                                "latest_span_at": (
                                    latest.isoformat() if latest else None
                                ),
                            },
                        ),
                    ),
                )
            )

        by_harness: dict[str, list[TelemetryAggregate]] = {}
        for aggregate in active:
            by_harness.setdefault(aggregate.harness_id, []).append(aggregate)
        for harness_id, items in sorted(by_harness.items()):
            if sum(i.token_observed_sessions for i in items) == 0:
                findings.append(
                    FindingCandidate(
                        analyzer_id=self.analyzer_id,
                        rule_id="telemetry.token_source_missing",
                        target_type="telemetry_source",
                        target_id=f"fleet/{harness_id}",
                        analyzer_class=AnalyzerClass.DETERMINISTIC,
                        severity=Severity.MEDIUM,
                        confidence=Confidence.CONFIRMED,
                        title=f"No token source for {harness_id}",
                        impact="Sessions from this harness carry no token accounting, so fleet token totals under-count.",
                        remediation=(
                            f"The {harness_id} harness reports no usage in "
                            "any recorded event; if it can emit usage, "
                            "enable it, otherwise expect this gap.",
                        ),
                        evidence=(
                            FindingEvidence(
                                source_ref=f"analytics:fleet/{harness_id}",
                                observed_at=snapshot.analyzed_at,
                                fields={
                                    "total_sessions": sum(
                                        i.total_sessions for i in items
                                    ),
                                    "hosts": sorted({i.host_id for i in items}),
                                },
                            ),
                        ),
                    )
                )

        rule = _COVERAGE_RULES[0]
        for aggregate in sorted(active, key=lambda item: item.target_id):
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
                    remediation=(rule.remediation.format(target=aggregate.target_id),),
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


def _reusable_input(harness_id: str, prompt_tokens: int, cache_read_tokens: int) -> int:
    """Total input a cache could have served, in the harness's own token accounting.

    Most harnesses report cache reads alongside input, so the two add up. Codex
    reports them inside it -- see ``CACHE_INSIDE_INPUT_HARNESSES``, which exists
    for exactly this -- and adding them there inflates the denominator and pushes a
    healthy target under the floor. A codex session with 100,000 input and 11,000
    cached reads is at 11%, comfortably above the 10% minimum, but summing gives
    11,000/111,000 = 9.9% and raises a finding against a target doing nothing wrong.

    ``cockpit/analytics.py`` already makes this distinction; the numbers here have
    to agree with the ones a reader sees there.
    """
    if harness_id in CACHE_INSIDE_INPUT_HARNESSES:
        return prompt_tokens
    return prompt_tokens + cache_read_tokens


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
            if not aggregate.facts_complete:
                continue
            prompt_tokens = (
                aggregate.exact_cache_metric_pair_prompt_tokens
                + aggregate.span_cache_metric_pair_prompt_tokens
            )
            cache_read_tokens = (
                aggregate.exact_cache_metric_pair_cache_read_tokens
                + aggregate.span_cache_metric_pair_cache_read_tokens
            )
            reusable_input = _reusable_input(
                aggregate.harness_id, prompt_tokens, cache_read_tokens
            )
            if reusable_input < self.minimum_input_tokens or reusable_input == 0:
                continue
            cache_percent = _percent(cache_read_tokens, reusable_input)
            if cache_percent >= self.minimum_cache_read_percent:
                continue
            # Confidence turns on whether the span-derived part of the ratio is
            # load-bearing, not on whether any span record exists. If the exact
            # numbers alone already fall below the floor, the span data cannot
            # change the verdict and the finding is CONFIRMED; a target with 500
            # exact sessions and one span-only session should not be downgraded by
            # the latter. Only where the exact numbers alone would not have raised
            # the finding is the span contribution actually carrying it.
            exact_reusable_input = _reusable_input(
                aggregate.harness_id,
                aggregate.exact_cache_metric_pair_prompt_tokens,
                aggregate.exact_cache_metric_pair_cache_read_tokens,
            )
            exact_alone_is_sufficient = (
                exact_reusable_input >= self.minimum_input_tokens
                and exact_reusable_input > 0
                and _percent(
                    aggregate.exact_cache_metric_pair_cache_read_tokens,
                    exact_reusable_input,
                )
                < self.minimum_cache_read_percent
            )
            has_span_pairs = aggregate.span_cache_metric_pair_records > 0
            span_evidence_is_load_bearing = (
                has_span_pairs and not exact_alone_is_sufficient
            )
            sources = []
            if aggregate.exact_cache_metric_pair_sessions:
                sources.append("exact_session_usage")
            if has_span_pairs:
                sources.append("span")
            findings.append(
                FindingCandidate(
                    analyzer_id=self.analyzer_id,
                    rule_id="telemetry.cache_read_inefficiency",
                    target_type="telemetry_source",
                    target_id=aggregate.target_id,
                    analyzer_class=AnalyzerClass.DETERMINISTIC,
                    severity=Severity.MEDIUM,
                    confidence=(
                        Confidence.LIKELY
                        if span_evidence_is_load_bearing
                        else Confidence.CONFIRMED
                    ),
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
                                "measured_prompt_tokens": prompt_tokens,
                                "measured_cache_read_tokens": cache_read_tokens,
                                "reusable_input_tokens": reusable_input,
                                "cache_read_percent": cache_percent,
                                "minimum_cache_read_percent": self.minimum_cache_read_percent,
                                "cache_metric_sources": sources,
                                "exact_cache_metric_pair_sessions": (
                                    aggregate.exact_cache_metric_pair_sessions
                                ),
                                "span_cache_metric_pair_records": (
                                    aggregate.span_cache_metric_pair_records
                                ),
                            },
                        ),
                    ),
                )
            )
        return findings
