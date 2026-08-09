"""Strict contracts shared by advisory analyzers and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping, TypeAlias

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | Mapping[str, "JSONValue"]
)


class AnalyzerClass(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Confidence(StrEnum):
    CONFIRMED = "confirmed"
    LIKELY = "likely"
    SPECULATIVE = "speculative"


class FindingState(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"
    REGRESSED = "regressed"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True)
class FindingEvidence:
    source_ref: str
    observed_at: datetime
    fields: Mapping[str, JSONValue]
    excerpt: str | None = None

    def __post_init__(self) -> None:
        if not self.source_ref.strip():
            raise ValueError("source_ref is required")
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class FindingCandidate:
    analyzer_id: str
    rule_id: str
    target_type: str
    target_id: str
    analyzer_class: AnalyzerClass
    severity: Severity
    confidence: Confidence
    title: str
    impact: str
    remediation: tuple[str, ...]
    evidence: tuple[FindingEvidence, ...]
    content_hash: str | None = None

    def __post_init__(self) -> None:
        enum_fields = {
            "analyzer_class": AnalyzerClass,
            "severity": Severity,
            "confidence": Confidence,
        }
        for name, enum_type in enum_fields.items():
            if not isinstance(getattr(self, name), enum_type):
                raise ValueError(f"{name} must be a {enum_type.__name__} value")
        for name in ("analyzer_id", "rule_id", "target_type", "target_id", "title"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if (
            self.analyzer_class == AnalyzerClass.MODEL
            and self.confidence == Confidence.CONFIRMED
        ):
            raise ValueError("model findings cannot be confirmed")
        object.__setattr__(self, "remediation", tuple(self.remediation))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.remediation or not all(step.strip() for step in self.remediation):
            raise ValueError("at least one remediation step is required")
        if not self.evidence:
            raise ValueError("at least one evidence record is required")


@dataclass(frozen=True)
class Finding:
    finding_id: str
    fingerprint: str
    analyzer_id: str
    rule_id: str
    target_type: str
    target_id: str
    analyzer_class: AnalyzerClass
    severity: Severity
    confidence: Confidence
    title: str
    impact: str
    remediation: tuple[str, ...]
    state: FindingState
    dismissal_reason: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    dismissed_at: datetime | None
    regressed_at: datetime | None
    evaluated_content_hash: str | None
    latest_run_id: str
