"""Shared read-only classification for retired advisory evidence."""

from drover.server.advisory.types import Finding


def retired_rule_ids(analyzer_id: str, rule_id: str) -> bool:
    return (
        analyzer_id == "deterministic.telemetry_coverage"
        and rule_id == "telemetry.flow_coverage"
    )


def is_retired_rule(finding: Finding) -> bool:
    return retired_rule_ids(finding.analyzer_id, finding.rule_id)
