"""Metadata-only insights for events the hub cannot accept from a host."""

from datetime import datetime, timezone
from pathlib import Path

from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    FindingCandidate,
    FindingEvidence,
    Severity,
)


def record_ownership_rejection(
    path: Path, host_id: str, session_id: str, reason: str
) -> None:
    AdvisoryRepository(path).observe(
        FindingCandidate(
            analyzer_id="deterministic.event_delivery",
            rule_id="events.ownership_rejected",
            target_type="host",
            target_id=host_id,
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.MEDIUM,
            confidence=Confidence.CONFIRMED,
            title="Structured events rejected for session ownership",
            impact="The host must quarantine rejected history; these events cannot update the hub session.",
            remediation=(
                "Inspect harness_event_quarantine on the host for the retained event IDs and reasons. "
                "Verify session ownership before explicitly replaying any quarantined history.",
            ),
            evidence=(
                FindingEvidence(
                    source_ref=f"harness:{host_id}/{session_id}",
                    observed_at=datetime.now(timezone.utc),
                    fields={
                        "host_id": host_id,
                        "session_id": session_id,
                        "reason": reason,
                    },
                ),
            ),
        ),
        run_id="event-ownership-rejection",
    )
