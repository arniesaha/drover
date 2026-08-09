from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.service import InsightFilters, InsightsService
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


def test_excerpt_redacts_basic_authorization_and_private_keys(repository, candidate):
    secret_candidate = replace(
        candidate,
        evidence=(
            replace(
                candidate.evidence[0],
                excerpt=(
                    "Authorization: Basic basic-credential\n"
                    "private_key=private-key-value"
                ),
            ),
        ),
    )

    finding = repository.observe(secret_candidate, run_id="run-secret-forms")
    con = duckdb.connect(str(repository.duckdb_path), read_only=True)
    try:
        excerpt = con.execute(
            "SELECT excerpt FROM advisory_occurrences WHERE finding_id = ?",
            [finding.finding_id],
        ).fetchone()[0]
    finally:
        con.close()

    assert "basic-credential" not in excerpt
    assert "private-key-value" not in excerpt
    assert excerpt.count("[REDACTED]") == 2


def test_finding_text_is_bounded_and_redacted_before_persistence(repository, candidate):
    secret_candidate = replace(
        candidate,
        title="Authorization: Basic title-credential",
        impact="private_key=impact-private-key",
        remediation=("Set token=remediation-token in the protected store.",),
    )

    finding = repository.observe(secret_candidate, run_id="run-finding-text")
    persisted = " ".join((finding.title, finding.impact, *finding.remediation))

    assert "title-credential" not in persisted
    assert "impact-private-key" not in persisted
    assert "remediation-token" not in persisted
    assert persisted.count("[REDACTED]") == 3


def test_compound_sensitive_keys_are_redacted_everywhere(repository, candidate):
    secret_candidate = replace(
        candidate,
        title="client_secret=title-secret",
        impact="access_token=impact-secret",
        remediation=(
            "Replace refresh_token=refresh-secret.",
            "Clear session_token=session-secret.",
        ),
        evidence=(
            replace(
                candidate.evidence[0],
                fields={
                    "access_token": "metadata-access-secret",
                    "nested": {"client_secret": "metadata-client-secret"},
                },
                excerpt=(
                    "access_token=excerpt-access-secret\n"
                    "refresh_token=excerpt-refresh-secret\n"
                    "client_secret=excerpt-client-secret\n"
                    "session_token=excerpt-session-secret"
                ),
            ),
        ),
    )

    finding = repository.observe(secret_candidate, run_id="run-compound-secrets")
    con = duckdb.connect(str(repository.duckdb_path), read_only=True)
    try:
        evidence_json, excerpt = con.execute(
            "SELECT evidence_json, excerpt FROM advisory_occurrences "
            "WHERE finding_id = ?",
            [finding.finding_id],
        ).fetchone()
    finally:
        con.close()

    persisted = " ".join(
        (finding.title, finding.impact, *finding.remediation, evidence_json, excerpt)
    )
    for secret in (
        "title-secret",
        "impact-secret",
        "refresh-secret",
        "session-secret",
        "metadata-access-secret",
        "metadata-client-secret",
        "excerpt-access-secret",
        "excerpt-refresh-secret",
        "excerpt-client-secret",
        "excerpt-session-secret",
    ):
        assert secret not in persisted
    assert json.loads(evidence_json) == {
        "access_token": "[REDACTED]",
        "nested": {"client_secret": "[REDACTED]"},
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"title": "x" * 241}, "title exceeds 240"),
        ({"impact": "x" * 1201}, "impact exceeds 1200"),
        ({"remediation": ("x" * 1001,)}, "remediation step exceeds 1000"),
        ({"remediation": tuple("step" for _ in range(17))}, "remediation exceeds 16"),
    ],
)
def test_finding_text_rejects_oversized_values(candidate, changes, message):
    with pytest.raises(ValueError, match=message):
        replace(candidate, **changes)


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


def test_insights_service_paginates_by_severity_recency_and_id(repository, candidate):
    medium = repository.observe(
        replace(
            candidate,
            rule_id="hook.medium",
            severity=Severity.MEDIUM,
            evidence=(
                replace(
                    candidate.evidence[0],
                    observed_at=datetime(2026, 8, 8, 19, tzinfo=timezone.utc),
                ),
            ),
        ),
        run_id="run-medium",
    )
    older_high = repository.observe(candidate, run_id="run-high-old")
    newer_high = repository.observe(
        replace(
            candidate,
            rule_id="hook.high-new",
            evidence=(
                replace(
                    candidate.evidence[0],
                    observed_at=datetime(2026, 8, 8, 20, tzinfo=timezone.utc),
                ),
            ),
        ),
        run_id="run-high-new",
    )
    service = InsightsService(repository.duckdb_path)

    first = service.list_insights(InsightFilters(limit=2))
    second = service.list_insights(InsightFilters(limit=2, cursor=first["next_cursor"]))

    assert [item["finding_id"] for item in first["findings"]] == [
        newer_high.finding_id,
        older_high.finding_id,
    ]
    assert [item["finding_id"] for item in second["findings"]] == [medium.finding_id]
    assert second["next_cursor"] is None


def test_insights_service_filters_and_returns_bounded_redacted_detail(
    repository, candidate
):
    candidate = replace(candidate, target_id="mac-mini/codex/session-start")
    finding = repository.observe(candidate, run_id="run-1")
    service = InsightsService(repository.duckdb_path)

    page = service.list_insights(
        InsightFilters(
            state="open",
            severity="high",
            confidence="confirmed",
            analyzer_class="deterministic",
            host="mac-mini",
            harness="codex",
            target_type="hook",
            target_id="mac-mini/codex/session-start",
        )
    )
    detail = service.get_insight(finding.finding_id)

    assert [item["finding_id"] for item in page["findings"]] == [finding.finding_id]
    assert detail["finding"]["remediation"] == [
        "Restore executable /opt/drover/bin/session-start."
    ]
    assert detail["evidence"] == [
        {
            "observed_at": "2026-08-08T17:00:00+00:00",
            "source_ref": "host:mac-mini/hooks/session-start",
            "fields": {"exists": False, "exit_code": 127},
            "excerpt": "exec: /opt/drover/bin/session-start",
        }
    ]
