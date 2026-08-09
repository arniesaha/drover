"""Deterministic routing outcome analyzers."""

from __future__ import annotations

from drover.server.advisory.analyzers import AnalysisSnapshot
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    FindingCandidate,
    FindingEvidence,
    Severity,
)


class RoutingMismatchAnalyzer:
    analyzer_id = "deterministic.routing_mismatch"

    def __init__(
        self,
        *,
        minimum_decisions: int = 10,
        maximum_mismatch_percent: float = 10,
    ) -> None:
        if minimum_decisions <= 0:
            raise ValueError("minimum_decisions must be positive")
        if not 0 <= maximum_mismatch_percent <= 100:
            raise ValueError("maximum_mismatch_percent must be within [0, 100]")
        self.minimum_decisions = minimum_decisions
        self.maximum_mismatch_percent = maximum_mismatch_percent

    def analyze(self, snapshot: AnalysisSnapshot) -> list[FindingCandidate]:
        findings: list[FindingCandidate] = []
        for aggregate in sorted(snapshot.routing, key=lambda item: item.target_id):
            if aggregate.decision_count < self.minimum_decisions:
                continue
            mismatch_percent = round(
                (aggregate.mismatch_count / aggregate.decision_count) * 100, 2
            )
            if mismatch_percent <= self.maximum_mismatch_percent:
                continue
            findings.append(
                FindingCandidate(
                    analyzer_id=self.analyzer_id,
                    rule_id="routing.mismatch_frequency",
                    target_type="routing_policy",
                    target_id=aggregate.target_id,
                    analyzer_class=AnalyzerClass.DETERMINISTIC,
                    severity=Severity.HIGH,
                    confidence=Confidence.CONFIRMED,
                    title="Routing outcomes frequently differ from declared routes",
                    impact="Unexpected provider or model selection can change capability, latency, and API cost.",
                    remediation=(
                        f"Review routing policy and fallback events for provider {aggregate.provider} on {aggregate.harness_id} at host {aggregate.host_id}, correct the declared route outside Drover if needed, then run Check Again.",
                    ),
                    evidence=(
                        FindingEvidence(
                            source_ref=aggregate.source_ref,
                            observed_at=aggregate.observed_at,
                            fields={
                                "decision_count": aggregate.decision_count,
                                "mismatch_count": aggregate.mismatch_count,
                                "mismatch_percent": mismatch_percent,
                                "maximum_mismatch_percent": self.maximum_mismatch_percent,
                            },
                        ),
                    ),
                )
            )
        return findings
