"""Deterministic checks over canonical, caller-allowlisted hook descriptors."""

from __future__ import annotations

from drover.server.advisory.analyzers import AnalysisSnapshot, HookDescriptor
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    FindingCandidate,
    FindingEvidence,
    Severity,
)


class HookValidityAnalyzer:
    analyzer_id = "deterministic.hook_validity"

    def analyze(self, snapshot: AnalysisSnapshot) -> list[FindingCandidate]:
        findings: list[FindingCandidate] = []
        for hook in sorted(
            snapshot.hooks,
            key=lambda item: (item.host_id, item.harness_id, item.hook_id),
        ):
            if not hook.enabled:
                continue
            rule = self._failed_rule(hook)
            if rule is None:
                continue
            rule_id, title, remediation = rule
            findings.append(
                FindingCandidate(
                    analyzer_id=self.analyzer_id,
                    rule_id=rule_id,
                    target_type="hook",
                    target_id=f"{hook.host_id}/{hook.harness_id}/{hook.hook_id}",
                    analyzer_class=AnalyzerClass.DETERMINISTIC,
                    severity=Severity.HIGH,
                    confidence=Confidence.CONFIRMED,
                    title=title,
                    impact="The enabled harness hook cannot run, so expected lifecycle telemetry or automation may be absent.",
                    remediation=(remediation,),
                    evidence=(
                        FindingEvidence(
                            source_ref=hook.source_ref,
                            observed_at=hook.observed_at,
                            fields={
                                "enabled": hook.enabled,
                                "executable_exists": hook.executable_exists,
                                "executable_is_file": hook.executable_is_file,
                                "executable_is_executable": hook.executable_is_executable,
                                "target_hash": hook.target_hash,
                            },
                        ),
                    ),
                )
            )
        return findings

    @staticmethod
    def _failed_rule(hook: HookDescriptor) -> tuple[str, str, str] | None:
        location = hook.canonical_executable_path
        if not hook.executable_exists:
            return (
                "hook.missing_executable",
                f"{hook.hook_id} hook executable is missing",
                f"Restore executable {location} for the {hook.hook_id} hook on host {hook.host_id}, verify it is allowlisted, then run Check Again.",
            )
        if not hook.executable_is_file:
            return (
                "hook.executable_not_file",
                f"{hook.hook_id} hook target is not a file",
                f"Replace {location} with a regular executable file for the {hook.hook_id} hook on host {hook.host_id}, then run Check Again.",
            )
        if not hook.executable_is_executable:
            return (
                "hook.not_executable",
                f"{hook.hook_id} hook target is not executable",
                f"Grant execute permission to {location} for the {hook.hook_id} hook on host {hook.host_id}, then run Check Again.",
            )
        return None
