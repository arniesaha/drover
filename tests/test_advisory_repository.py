from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    FindingCandidate,
    FindingEvidence,
    FindingState,
    Severity,
)


@pytest.fixture
def repository(tmp_path):
    db_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db_path)
    return AdvisoryRepository(db_path)


@pytest.fixture
def candidate():
    return FindingCandidate(
        analyzer_id="hooks",
        rule_id="hook.executable_missing",
        target_type="hook",
        target_id="mac-mini:session-start",
        analyzer_class=AnalyzerClass.DETERMINISTIC,
        severity=Severity.HIGH,
        confidence=Confidence.CONFIRMED,
        title="SessionStart hook executable is missing",
        impact="New sessions skip required setup.",
        remediation=("Restore executable /opt/drover/bin/session-start.",),
        evidence=(
            FindingEvidence(
                source_ref="host:mac-mini/hooks/session-start",
                observed_at=datetime(2026, 8, 8, 17, tzinfo=timezone.utc),
                fields={"exists": False, "exit_code": 127},
                excerpt="exec: /opt/drover/bin/session-start",
            ),
        ),
        content_hash="hash-v1",
    )


def test_model_candidate_cannot_be_confirmed(candidate):
    with pytest.raises(ValueError, match="model findings cannot be confirmed"):
        replace(candidate, analyzer_class=AnalyzerClass.MODEL)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("analyzer_class", "deterministic"),
        ("severity", "high"),
        ("confidence", "confirmed"),
    ],
)
def test_candidate_requires_enum_values(candidate, field, value):
    with pytest.raises(ValueError, match=f"{field} must be"):
        replace(candidate, **{field: value})


def test_observation_deduplicates_and_regresses(repository, candidate):
    first = repository.observe(candidate, run_id="run-1")
    repository.mark_passing(first.finding_id, run_id="run-2")
    again = repository.observe(candidate, run_id="run-3")

    assert again.finding_id == first.finding_id
    assert again.state == FindingState.REGRESSED
    assert len(repository.list_findings()) == 1


def test_fingerprint_ignores_content_and_finding_details(repository, candidate):
    first = repository.observe(candidate, run_id="run-1")
    changed = replace(
        candidate,
        content_hash="hash-v2",
        title="Updated title",
        severity=Severity.CRITICAL,
    )

    assert repository.observe(changed, run_id="run-2").finding_id == first.finding_id


def test_dismissed_finding_only_reopens_for_material_change(repository, candidate):
    finding = repository.observe(candidate, run_id="run-1")
    repository.dismiss(finding.finding_id, reason="accepted tradeoff")

    ordinary = replace(
        candidate,
        evidence=(
            replace(
                candidate.evidence[0],
                observed_at=datetime(2026, 8, 8, 18, tzinfo=timezone.utc),
            ),
        ),
    )
    assert repository.observe(ordinary, run_id="run-2").state == FindingState.DISMISSED

    changed = replace(candidate, content_hash="new-hash")
    reopened = repository.observe(changed, run_id="run-3")
    assert reopened.state == FindingState.OPEN
    assert reopened.dismissal_reason is None


def test_dismissed_finding_reopens_for_higher_severity_or_material_evidence(
    repository, candidate
):
    lower = replace(candidate, severity=Severity.MEDIUM)
    finding = repository.observe(lower, run_id="run-1")
    repository.dismiss(finding.finding_id, reason="accepted tradeoff")
    assert repository.observe(candidate, run_id="run-2").state == FindingState.OPEN

    repository.dismiss(finding.finding_id, reason="still accepted")
    evidence_changed = replace(
        candidate,
        evidence=(
            replace(candidate.evidence[0], fields={"exists": False, "exit_code": 126}),
        ),
    )
    assert (
        repository.observe(evidence_changed, run_id="run-3").state == FindingState.OPEN
    )


def test_acknowledge_and_dismiss_require_valid_transitions(repository, candidate):
    finding = repository.observe(candidate, run_id="run-1")
    assert repository.acknowledge(finding.finding_id).state == FindingState.ACKNOWLEDGED
    with pytest.raises(ValueError, match="reason is required"):
        repository.dismiss(finding.finding_id, reason="  ")
    assert (
        repository.dismiss(finding.finding_id, reason="accepted tradeoff").state
        == FindingState.DISMISSED
    )


def test_mark_passing_is_the_only_resolution_path(repository, candidate):
    finding = repository.observe(candidate, run_id="run-1")
    repository.acknowledge(finding.finding_id)
    resolved = repository.mark_passing(finding.finding_id, run_id="run-pass")

    assert resolved.state == FindingState.RESOLVED
    assert resolved.resolved_at is not None
    with pytest.raises(ValueError, match="cannot acknowledge"):
        repository.acknowledge(finding.finding_id)


def test_observe_appends_bounded_redacted_occurrence_atomically(repository, candidate):
    secret_candidate = replace(
        candidate,
        evidence=(
            replace(
                candidate.evidence[0],
                fields={"authorization": "Bearer secret-value", "exists": False},
                excerpt="Authorization: Bearer secret-value token=second-secret",
            ),
        ),
    )
    finding = repository.observe(secret_candidate, run_id="run-secret")

    con = duckdb.connect(str(repository.duckdb_path), read_only=True)
    try:
        row = con.execute(
            "SELECT evidence_json, excerpt FROM advisory_occurrences "
            "WHERE finding_id = ?",
            [finding.finding_id],
        ).fetchone()
        finding_count = con.execute(
            "SELECT count(*) FROM advisory_findings WHERE finding_id = ?",
            [finding.finding_id],
        ).fetchone()[0]
    finally:
        con.close()

    assert finding_count == 1
    assert "secret-value" not in row[0]
    assert json.loads(row[0])["authorization"] == "[REDACTED]"
    assert "secret-value" not in row[1]
    assert "second-secret" not in row[1]


def test_rejects_oversized_or_full_content_evidence(repository, candidate):
    oversized = replace(
        candidate,
        evidence=(replace(candidate.evidence[0], excerpt="x" * 513),),
    )
    with pytest.raises(ValueError, match="excerpt exceeds"):
        repository.observe(oversized, run_id="run-large")

    full_content = replace(
        candidate,
        evidence=(replace(candidate.evidence[0], fields={"full_content": "secret"}),),
    )
    with pytest.raises(ValueError, match="full configuration content"):
        repository.observe(full_content, run_id="run-content")


def test_observe_rolls_back_finding_when_occurrence_insert_fails(
    repository, candidate, monkeypatch
):
    monkeypatch.setattr(
        repository, "_insert_occurrences", lambda *args, **kwargs: 1 / 0
    )

    with pytest.raises(ZeroDivisionError):
        repository.observe(candidate, run_id="run-1")

    assert repository.list_findings() == []
