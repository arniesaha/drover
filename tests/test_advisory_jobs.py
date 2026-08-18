"""Durable scheduling and isolated execution for advisory analyzers."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from drover.config import AdvisoryContentConfig, default_config, load_config
from drover.schema import bootstrap
from drover.server.__main__ import _create_content_analysis_worker
from drover.server.advisory.analyzers import (
    AnalysisSnapshot,
    ProviderConnectionObservation,
    ProviderResetWindow,
    TelemetryAggregate,
)
from drover.server.advisory.analyzers.connectors import ConnectorFreshnessAnalyzer
from drover.server.advisory.content_targets import BundledTarget, ContentBundle
from drover.server.advisory.jobs import (
    AdvisoryScheduler,
    enqueue_advisory_check,
    enqueue_operational_checks,
)
from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.service import (
    InsightsService,
    InvalidInsightTransition,
)
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    FindingCandidate,
    FindingEvidence,
    Severity,
)
from drover.server.advisory.worker import (
    OPERATIONAL_SPAN_LOOKBACK,
    AdvisoryWorker,
    ContentAnalysisScheduler,
    ContentAnalysisWorker,
    load_operational_snapshot,
    operational_analyzers,
    operational_snapshot_source_version,
)
from drover.server.cockpit.service import ProviderRefreshLoop
from drover.server.db import control_plane_path
from drover.server.harness.models import HarnessHost
from drover.server.harness.registry import HarnessRegistry
from drover.server.ledger import Ledger
from drover.server.providers.service import ProviderUsageService
from drover.server.providers.types import ProviderAccountSnapshot

NOW = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)


# Most fixtures here sit at NOW and hand it back as `analyzed_at`, which keeps
# them reproducible: same store, same analyzed_at, same facts, whatever the
# date is when the suite runs.
#
# A few production paths deliberately read the real clock instead -- the Check
# Again scope probe asks whether facts exist *now*, and taking an analyzed_at
# from a caller would defeat the question. Fixtures for those have to be
# current by construction. Pinning them to a date meant they passed until that
# date fell out of the seven day lookback window, which is exactly what
# happened at 2026-08-15 18:00 UTC, one wave after the capping test.
def _current_moment() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=1)


def _control_plane_execute(db_path: Path, sql: str, params: list | None = None) -> None:
    """Write control-plane rows where they live since #95.

    ``harness_sessions`` and ``harness_hosts`` moved out of the lakehouse into
    their own database. ``load_operational_snapshot`` reads them back through a
    private copy of that store, so the fixtures have to write to the real one.
    """
    with duckdb.connect(str(control_plane_path(db_path))) as con:
        con.execute(sql, params or [])


def _content_config(*, enabled: bool = True, external_consent: bool = False):
    return AdvisoryContentConfig(
        enabled=enabled,
        backend_policy="cloud" if external_consent else "local",
        external_consent=external_consent,
        targets=("/allowed/global-agents",),
        allowed_roots=(Path("/allowed"),),
        max_file_bytes=1024,
        max_bundle_bytes=4096,
        excerpt_max_chars=320,
    )


def _content_bundle(
    content: str,
    *,
    target_id: str = "global-agents",
    host_id: str = "mac-mini",
) -> ContentBundle:
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    bundle_hash = hashlib.sha256(
        json.dumps([(target_id, content_hash)], separators=(",", ":")).encode()
    ).hexdigest()
    return ContentBundle(
        host_id=host_id,
        created_at=NOW,
        bundle_hash=bundle_hash,
        targets=(
            BundledTarget(
                target_id=target_id,
                content_hash=content_hash,
                redacted_content=content,
            ),
        ),
    )


def _model_candidate(
    target_id: str,
    *,
    rule_id: str = "prompt.repetition",
    content_hash: str = "old-target-hash",
) -> FindingCandidate:
    return FindingCandidate(
        analyzer_id="model.configuration",
        rule_id=rule_id,
        target_type="configuration_target",
        target_id=f"mac-mini/{target_id}",
        analyzer_class=AnalyzerClass.MODEL,
        severity=Severity.MEDIUM,
        confidence=Confidence.LIKELY,
        title="Repeated instruction",
        impact="Repeated text consumes context.",
        remediation=("Keep one copy.",),
        evidence=(
            FindingEvidence(
                source_ref=f"content:mac-mini/{target_id}#old",
                observed_at=NOW,
                fields={"bundle_hash": "old"},
                excerpt="Repeated instruction",
            ),
        ),
        content_hash=content_hash,
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "lake", duckdb_path=path)
    return path


class HealthyAnalyzer:
    analyzer_id = "healthy"

    def analyze(self, snapshot: AnalysisSnapshot) -> list[FindingCandidate]:
        return [
            FindingCandidate(
                analyzer_id=self.analyzer_id,
                rule_id="healthy.rule",
                target_type="host",
                target_id="mac-mini",
                analyzer_class=AnalyzerClass.DETERMINISTIC,
                severity=Severity.LOW,
                confidence=Confidence.CONFIRMED,
                title="A deterministic issue",
                impact="The issue has a bounded operational impact.",
                remediation=("Repair it outside Drover, then run Check Again.",),
                evidence=(
                    FindingEvidence(
                        source_ref="test:healthy",
                        observed_at=snapshot.analyzed_at,
                        fields={"count": 1},
                    ),
                ),
                content_hash=snapshot.source_version,
            )
        ]


class ExplodingAnalyzer:
    analyzer_id = "exploding"

    def analyze(self, snapshot: AnalysisSnapshot) -> list[FindingCandidate]:
        raise RuntimeError("isolated analyzer failure")


class PassingAnalyzer:
    analyzer_id = "healthy"

    def analyze(self, snapshot: AnalysisSnapshot) -> list[FindingCandidate]:
        return []


class HealthyTelemetryAnalyzer:
    analyzer_id = "healthy"

    def analyze(self, snapshot: AnalysisSnapshot) -> list[FindingCandidate]:
        return [
            FindingCandidate(
                analyzer_id=self.analyzer_id,
                rule_id="telemetry.test",
                target_type="telemetry_source",
                target_id="mac-mini/codex",
                analyzer_class=AnalyzerClass.DETERMINISTIC,
                severity=Severity.LOW,
                confidence=Confidence.CONFIRMED,
                title="Telemetry issue",
                impact="Telemetry is incomplete.",
                remediation=("Repair telemetry outside Drover.",),
                evidence=(
                    FindingEvidence(
                        source_ref="test:telemetry",
                        observed_at=snapshot.analyzed_at,
                        fields={"count": 1},
                    ),
                ),
            )
        ]


def _snapshot(source_version: str) -> AnalysisSnapshot:
    return AnalysisSnapshot(source_version=source_version, analyzed_at=NOW)


def _telemetry_snapshot(source_version: str) -> AnalysisSnapshot:
    return AnalysisSnapshot(
        source_version=source_version,
        analyzed_at=NOW,
        telemetry=(
            TelemetryAggregate(
                target_id="mac-mini/codex",
                host_id="mac-mini",
                harness_id="codex",
                observed_at=NOW,
                total_sessions=1,
                sessions_with_spans=1,
                repository_attributed_sessions=1,
                token_observed_sessions=1,
                cost_observed_sessions=1,
                prompt_tokens=1,
                cache_read_tokens=0,
                facts_complete=True,
                input_span_records=1,
                source_ref="test:telemetry",
            ),
        ),
    )


def test_same_target_hash_coalesces_to_one_job(db_path: Path) -> None:
    first = enqueue_advisory_check(
        db_path,
        analyzer_id="hooks",
        target_id="mac",
        source_version="abc",
    )
    second = enqueue_advisory_check(
        db_path,
        analyzer_id="hooks",
        target_id="mac",
        source_version="abc",
    )

    assert second.job_id == first.job_id
    with duckdb.connect(str(db_path)) as con:
        assert (
            con.execute(
                "SELECT subject_key FROM pipeline_jobs WHERE job_id = ?", [first.job_id]
            ).fetchone()[0]
            == "hooks:mac"
        )
        assert (
            con.execute(
                "SELECT count(*) FROM pipeline_receipts WHERE source_version = 'abc'"
            ).fetchone()[0]
            == 1
        )


def test_new_source_version_replays_completed_subject(db_path: Path) -> None:
    first = enqueue_advisory_check(
        db_path, analyzer_id="healthy", target_id="mac-mini", source_version="v1"
    )
    worker = AdvisoryWorker(
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
        snapshot_factory=lambda _analyzer, _target, version: _snapshot(version),
        worker_id="test-worker",
    )
    assert worker.run_once([HealthyAnalyzer()]).succeeded == 1

    second = enqueue_advisory_check(
        db_path, analyzer_id="healthy", target_id="mac-mini", source_version="v2"
    )

    assert second.job_id != first.job_id
    assert second.status == "pending"


def test_failed_analyzer_does_not_block_next_analyzer(db_path: Path) -> None:
    for analyzer_id in ("exploding", "healthy"):
        enqueue_advisory_check(
            db_path,
            analyzer_id=analyzer_id,
            target_id="mac-mini",
            source_version="snapshot-v1",
        )
    worker = AdvisoryWorker(
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
        snapshot_factory=lambda _analyzer, _target, version: _snapshot(version),
        worker_id="test-worker",
    )

    result = worker.run_once([ExplodingAnalyzer(), HealthyAnalyzer()])

    assert (result.failed, result.succeeded) == (1, 1)
    with duckdb.connect(str(db_path)) as con:
        jobs = dict(
            con.execute(
                "SELECT subject_key, status FROM pipeline_jobs ORDER BY subject_key"
            ).fetchall()
        )
        assert jobs == {
            "exploding:mac-mini": "retry_wait",
            "healthy:mac-mini": "succeeded",
        }
        attempts = con.execute(
            "SELECT result FROM pipeline_job_attempts ORDER BY started_at, attempt_id"
        ).fetchall()
        assert sorted(row[0] for row in attempts) == [
            "retryable_failed",
            "succeeded",
        ]
        artifact = con.execute(
            "SELECT artifact_kind, subject_key, metadata_json "
            "FROM pipeline_artifacts"
        ).fetchone()
        assert artifact[:2] == ("advisory_finding_batch", "healthy:mac-mini")
        assert len(json.loads(artifact[2])["finding_ids"]) == 1
        receipts = dict(
            con.execute(
                "SELECT source_key, status FROM pipeline_receipts ORDER BY source_key"
            ).fetchall()
        )
        assert receipts == {
            "exploding:mac-mini": "observed",
            "healthy:mac-mini": "applied",
        }


def test_empty_snapshot_is_not_treated_as_passing_evidence(db_path: Path) -> None:
    enqueue_advisory_check(
        db_path, analyzer_id="healthy", target_id="mac-mini", source_version="v1"
    )
    worker = AdvisoryWorker(
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
        snapshot_factory=lambda _analyzer, _target, version: _snapshot(version),
    )
    worker.run_once([HealthyAnalyzer()])
    enqueue_advisory_check(
        db_path,
        analyzer_id="healthy",
        target_id="mac-mini",
        source_version="v2",
    )

    worker.run_once([PassingAnalyzer()])

    assert AdvisoryRepository(db_path).list_findings()[0].state.value == "open"


def test_ledger_completion_failure_rolls_back_findings_and_retry_is_idempotent(
    db_path: Path, monkeypatch
) -> None:
    job = enqueue_advisory_check(
        db_path, analyzer_id="healthy", target_id="mac-mini", source_version="v1"
    )
    worker = AdvisoryWorker(
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
        snapshot_factory=lambda _analyzer, _target, version: _snapshot(version),
        retry_delay=timedelta(0),
        clock=lambda: NOW,
    )
    original = Ledger.succeed_job

    def fail_after_completion(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("fault after ledger completion")

    monkeypatch.setattr(Ledger, "succeed_job", fail_after_completion)
    assert worker.run_once([HealthyAnalyzer()]).failed == 1
    with duckdb.connect(str(db_path), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM advisory_findings").fetchone()[0] == 0
        assert (
            con.execute("SELECT count(*) FROM advisory_occurrences").fetchone()[0] == 0
        )
        assert con.execute("SELECT count(*) FROM pipeline_artifacts").fetchone()[0] == 0
        assert (
            con.execute(
                "SELECT status FROM pipeline_jobs WHERE job_id = ?", [job.job_id]
            ).fetchone()[0]
            == "retry_wait"
        )

    monkeypatch.setattr(Ledger, "succeed_job", original)
    assert worker.run_once([HealthyAnalyzer()]).succeeded == 1
    with duckdb.connect(str(db_path), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM advisory_findings").fetchone()[0] == 1
        assert (
            con.execute("SELECT count(*) FROM advisory_occurrences").fetchone()[0] == 1
        )
        assert con.execute("SELECT count(*) FROM pipeline_artifacts").fetchone()[0] == 1


def test_new_source_version_reopens_dead_lettered_subject(db_path: Path) -> None:
    enqueue_advisory_check(
        db_path,
        analyzer_id="exploding",
        target_id="mac-mini",
        source_version="v1",
        max_attempts=1,
    )
    worker = AdvisoryWorker(
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
        snapshot_factory=lambda _analyzer, _target, version: _snapshot(version),
    )
    worker.run_once([ExplodingAnalyzer()])

    next_job = enqueue_advisory_check(
        db_path,
        analyzer_id="exploding",
        target_id="mac-mini",
        source_version="v2",
    )

    assert next_job.status == "pending"


def test_stale_lease_is_reclaimed_before_analyzer_runs(db_path: Path) -> None:
    job = enqueue_advisory_check(
        db_path, analyzer_id="healthy", target_id="mac-mini", source_version="v1"
    )
    with duckdb.connect(str(db_path)) as con:
        Ledger(con).lease_job(
            job.job_id,
            worker_id="crashed-worker",
            lease_expires_at=NOW - timedelta(seconds=1),
        )
    worker = AdvisoryWorker(
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
        snapshot_factory=lambda _analyzer, _target, version: _snapshot(version),
        clock=lambda: NOW,
    )

    assert worker.run_once([HealthyAnalyzer()]).succeeded == 1
    with duckdb.connect(str(db_path)) as con:
        assert con.execute(
            "SELECT status, attempt_count FROM pipeline_jobs WHERE job_id = ?",
            [job.job_id],
        ).fetchone() == ("succeeded", 2)
        assert [
            row[0]
            for row in con.execute(
                "SELECT result FROM pipeline_job_attempts "
                "WHERE job_id = ? ORDER BY attempt_no",
                [job.job_id],
            ).fetchall()
        ] == ["retryable_failed", "succeeded"]


def test_expired_lease_at_attempt_cap_is_dead_lettered(db_path: Path) -> None:
    job = enqueue_advisory_check(
        db_path,
        analyzer_id="healthy",
        target_id="mac-mini",
        source_version="v1",
        max_attempts=1,
    )
    with duckdb.connect(str(db_path)) as con:
        Ledger(con).lease_job(
            job.job_id,
            worker_id="crashed-worker",
            lease_expires_at=NOW - timedelta(seconds=1),
        )
    worker = AdvisoryWorker(
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
        snapshot_factory=lambda _analyzer, _target, version: _snapshot(version),
        clock=lambda: NOW,
    )

    result = worker.run_once([HealthyAnalyzer()])

    assert (result.succeeded, result.skipped) == (0, 1)
    with duckdb.connect(str(db_path)) as con:
        assert con.execute(
            "SELECT status, attempt_count FROM pipeline_jobs WHERE job_id = ?",
            [job.job_id],
        ).fetchone() == ("dead_lettered", 1)
        assert con.execute(
            "SELECT result, error_category FROM pipeline_job_attempts "
            "WHERE job_id = ?",
            [job.job_id],
        ).fetchone() == ("terminal_failed", "lease_expired")


def test_passing_evidence_preserves_dismissed_finding(db_path: Path) -> None:
    enqueue_advisory_check(
        db_path, analyzer_id="healthy", target_id="mac-mini", source_version="v1"
    )
    repository = AdvisoryRepository(db_path)
    worker = AdvisoryWorker(
        duckdb_path=db_path,
        repository=repository,
        snapshot_factory=lambda _analyzer, _target, version: _telemetry_snapshot(
            version
        ),
    )
    worker.run_once([HealthyTelemetryAnalyzer()])
    finding = repository.dismiss(
        repository.list_findings()[0].finding_id, reason="accepted tradeoff"
    )
    enqueue_advisory_check(
        db_path, analyzer_id="healthy", target_id="mac-mini", source_version="v2"
    )

    worker.run_once([PassingAnalyzer()])

    after = repository.get_finding(finding.finding_id)
    assert after.state.value == "dismissed"
    assert after.dismissal_reason == "accepted tradeoff"


def test_operational_change_enqueues_only_lightweight_analyzers(db_path: Path) -> None:
    jobs = enqueue_operational_checks(
        db_path,
        target_id="mac-mini",
        source_version="operational-v4",
        analyzer_ids=("connectors", "telemetry"),
    )

    assert [job.subject_key for job in jobs] == [
        "connectors:mac-mini",
        "telemetry:mac-mini",
    ]


def test_disabled_content_job_never_fetches_bundle() -> None:
    calls: list[str] = []
    worker = ContentAnalysisWorker(
        consent_reader=lambda: _content_config(enabled=False),
        bundle_fetcher=lambda _host, _targets: calls.append("fetch"),
        backend_factory=lambda _config: calls.append("backend"),
    )

    result = worker.run_model_job(host_id="mac-mini", target_ids=("global-agents",))

    assert result.status == "disabled"
    assert calls == []


def test_content_job_rereads_consent_before_fetch_and_backend_creation() -> None:
    configs = iter(
        (
            _content_config(),
            _content_config(),
            _content_config(enabled=False),
        )
    )
    calls: list[str] = []

    class Backend:
        def complete(self, _system: str, _user: str) -> str:
            calls.append("complete")
            return '{"findings":[]}'

    bundle = _content_bundle("Review this instruction.")
    worker = ContentAnalysisWorker(
        consent_reader=lambda: next(configs),
        bundle_fetcher=lambda _host, _targets: (calls.append("fetch"), bundle)[1],
        backend_factory=lambda _config: (calls.append("backend"), Backend())[1],
    )

    result = worker.run_model_job(host_id="mac-mini", target_ids=("global-agents",))

    assert result.status == "revoked"
    assert calls == ["fetch"]


def test_content_job_rereads_consent_immediately_before_backend_call() -> None:
    configs = iter(
        (
            _content_config(),
            _content_config(),
            _content_config(),
            _content_config(enabled=False),
        )
    )
    calls: list[str] = []

    class Backend:
        def complete(self, _system: str, _user: str) -> str:
            calls.append("complete")
            return '{"findings":[]}'

    bundle = _content_bundle("Review this instruction.")
    worker = ContentAnalysisWorker(
        consent_reader=lambda: next(configs),
        bundle_fetcher=lambda _host, _targets: (calls.append("fetch"), bundle)[1],
        backend_factory=lambda _config: (calls.append("backend"), Backend())[1],
    )

    result = worker.run_model_job(host_id="mac-mini", target_ids=("global-agents",))

    assert result.status == "revoked"
    assert calls == ["fetch", "backend"]


@pytest.mark.parametrize("failing_stage", ["fetch", "backend"])
def test_content_job_exceptions_do_not_echo_request_or_config_content(
    failing_stage: str,
) -> None:
    sensitive = "private prompt content"

    def fetch(_host, _targets):
        if failing_stage == "fetch":
            raise RuntimeError(sensitive)
        return _content_bundle(sensitive)

    def backend_factory(_config):
        raise RuntimeError(sensitive)

    worker = ContentAnalysisWorker(
        consent_reader=lambda: _content_config(),
        bundle_fetcher=fetch,
        backend_factory=backend_factory,
    )

    with pytest.raises(ValueError) as captured:
        worker.run_model_job(host_id="mac-mini", target_ids=("global-agents",))

    assert sensitive not in str(captured.value)


@pytest.mark.parametrize(
    "bundle",
    [
        _content_bundle("content", target_id="unexpected"),
        ContentBundle(
            host_id="mac-mini",
            created_at=NOW,
            bundle_hash=hashlib.sha256(b"[]").hexdigest(),
            targets=(),
        ),
        ContentBundle(
            host_id="mac-mini",
            created_at=NOW,
            bundle_hash="b" * 64,
            targets=(
                BundledTarget(
                    target_id="global-agents",
                    content_hash="0" * 64,
                    redacted_content="content",
                ),
            ),
        ),
        replace(_content_bundle("content"), bundle_hash="0" * 64),
    ],
)
def test_content_job_rejects_unrequested_or_hash_mismatched_bundles(
    bundle: ContentBundle,
) -> None:
    worker = ContentAnalysisWorker(
        consent_reader=lambda: _content_config(),
        bundle_fetcher=lambda _host, _targets: bundle,
        backend_factory=lambda _config: pytest.fail("backend must not be created"),
    )

    with pytest.raises(ValueError, match="content bundle"):
        worker.run_model_job(host_id="mac-mini", target_ids=("global-agents",))


def test_content_job_rejects_unvalidated_mapping_bundle() -> None:
    worker = ContentAnalysisWorker(
        consent_reader=lambda: _content_config(),
        bundle_fetcher=lambda _host, _targets: {
            "bundle_hash": "b" * 64,
            "created_at": NOW.isoformat(),
            "targets": [],
            "extra": "not allowed",
        },
        backend_factory=lambda _config: pytest.fail("backend must not be created"),
    )

    with pytest.raises(ValueError, match="validated ContentBundle"):
        worker.run_model_job(host_id="mac-mini", target_ids=("global-agents",))


def test_content_job_returns_only_hashes_and_finding_ids() -> None:
    content = "Always inspect the repository. Always inspect the repository."
    bundle = _content_bundle(content)

    class Backend:
        def complete(self, _system: str, _user: str) -> str:
            return json.dumps(
                {
                    "findings": [
                        {
                            "rule_id": "prompt.repetition",
                            "target_id": "global-agents",
                            "severity": "medium",
                            "confidence": "likely",
                            "title": "Repeated instruction",
                            "impact": "Repeated text consumes context.",
                            "evidence_excerpt": "Always inspect the repository.",
                            "remediation": ["Keep one copy of the instruction."],
                        }
                    ]
                }
            )

    worker = ContentAnalysisWorker(
        consent_reader=lambda: _content_config(),
        bundle_fetcher=lambda _host, _targets: bundle,
        backend_factory=lambda _config: Backend(),
    )

    result = worker.run_model_job(host_id="mac-mini", target_ids=("global-agents",))

    encoded = json.dumps(result.artifact)
    assert result.status == "succeeded"
    assert result.artifact == {
        "bundle_hash": bundle.bundle_hash,
        "target_hashes": [bundle.targets[0].content_hash],
        "finding_ids": ["model.configuration:prompt.repetition:mac-mini/global-agents"],
    }
    assert content not in encoded


def test_content_job_persists_only_hashes_and_finding_ids_in_ledger(
    db_path: Path,
) -> None:
    content = "Always inspect the repository. Always inspect the repository."
    bundle = _content_bundle(content)
    job = enqueue_advisory_check(
        db_path,
        analyzer_id="model.configuration",
        target_id="mac-mini",
        source_version=bundle.bundle_hash,
    )
    with duckdb.connect(str(db_path)) as con:
        Ledger(con).lease_job(
            job.job_id,
            worker_id="content-test",
            lease_expires_at=NOW + timedelta(minutes=5),
        )
        job = Ledger(con).latest_job("analyze_advisory_target", job.subject_key)
    assert job is not None

    class Backend:
        def complete(self, _system: str, _user: str) -> str:
            return json.dumps(
                {
                    "findings": [
                        {
                            "rule_id": "prompt.repetition",
                            "target_id": "global-agents",
                            "severity": "medium",
                            "confidence": "likely",
                            "title": "Repeated instruction",
                            "impact": "Repeated text consumes context.",
                            "evidence_excerpt": "Always inspect the repository.",
                            "remediation": ["Keep one copy of the instruction."],
                        }
                    ]
                }
            )

    worker = ContentAnalysisWorker(
        consent_reader=lambda: _content_config(),
        bundle_fetcher=lambda _host, _targets: bundle,
        backend_factory=lambda _config: Backend(),
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
    )

    result = worker.run_model_job(
        host_id="mac-mini", target_ids=("global-agents",), job=job
    )

    assert result.status == "succeeded"
    with duckdb.connect(str(db_path), read_only=True) as con:
        metrics, metadata, receipt_status = con.execute(
            """
            SELECT a.metrics_json, p.metadata_json, r.status
            FROM pipeline_jobs j
            JOIN pipeline_job_attempts a ON a.attempt_id = j.latest_attempt_id
            JOIN pipeline_artifacts p ON p.artifact_id = j.latest_artifact_id
            JOIN pipeline_receipts r ON r.receipt_id = j.caused_by_receipt_id
            WHERE j.job_id = ?
            """,
            [job.job_id],
        ).fetchone()
    persisted = metrics + metadata
    assert content not in persisted
    assert set(json.loads(metrics)) == {
        "bundle_hash",
        "target_hashes",
        "finding_ids",
    }
    assert set(json.loads(metadata)) == {
        "bundle_hash",
        "target_hashes",
        "finding_ids",
    }
    assert receipt_status == "applied"


def test_content_worker_claims_pending_model_job_through_ledger(
    db_path: Path,
) -> None:
    bundle = _content_bundle("No issue here.")
    job = enqueue_advisory_check(
        db_path,
        analyzer_id="model.configuration",
        target_id="mac-mini",
        source_version=bundle.bundle_hash,
    )

    class Backend:
        def complete(self, _system: str, _user: str) -> str:
            return '{"findings":[]}'

    worker = ContentAnalysisWorker(
        consent_reader=lambda: _content_config(),
        bundle_fetcher=lambda _host, _targets: bundle,
        backend_factory=lambda _config: Backend(),
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
        worker_id="content-test",
    )

    result = worker.run_once()

    assert result.succeeded == 1
    with duckdb.connect(str(db_path), read_only=True) as con:
        assert (
            con.execute(
                "SELECT status FROM pipeline_jobs WHERE job_id = ?", [job.job_id]
            ).fetchone()[0]
            == "succeeded"
        )


def test_disabled_content_worker_leaves_pending_job_without_fetch(
    db_path: Path,
) -> None:
    job = enqueue_advisory_check(
        db_path,
        analyzer_id="model.configuration",
        target_id="mac-mini",
        source_version="content-v1",
    )
    calls: list[str] = []
    worker = ContentAnalysisWorker(
        consent_reader=lambda: _content_config(enabled=False),
        bundle_fetcher=lambda _host, _targets: calls.append("fetch"),
        backend_factory=lambda _config: calls.append("backend"),
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
    )

    result = worker.run_once()

    assert result.skipped == 1
    assert calls == []
    with duckdb.connect(str(db_path), read_only=True) as con:
        assert (
            con.execute(
                "SELECT status FROM pipeline_jobs WHERE job_id = ?", [job.job_id]
            ).fetchone()[0]
            == "pending"
        )


def test_revoke_waits_for_scheduler_probe_then_cancels_post_probe_job(
    db_path: Path, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("""[advisory_content]
enabled = true
backend_policy = "local"
external_consent = false
targets = ["/allowed/global-agents"]
allowed_roots = ["/allowed"]
max_file_bytes = 1024
max_bundle_bytes = 4096
excerpt_max_chars = 320
""")
    entered = threading.Event()
    release = threading.Event()
    revoked = threading.Event()
    probes: list[str] = []
    bundle = _content_bundle("No issue.")

    class Registry:
        def list_hosts(self):
            return [type("Host", (), {"host_id": "mac-mini"})()]

    def probe(host_id, _target_ids):
        probes.append(host_id)
        entered.set()
        assert release.wait(2)
        return bundle.bundle_hash

    scheduler = ContentAnalysisScheduler(
        duckdb_path=db_path,
        registry=Registry(),
        consent_reader=lambda: load_config(config_path).advisory_content,
        version_fetcher=probe,
        interval_seconds=300,
        clock=lambda: 1_000,
    )
    scheduling = threading.Thread(target=scheduler.enqueue_due)
    scheduling.start()
    assert entered.wait(2)

    service = InsightsService(db_path, config_path=config_path)

    def revoke() -> None:
        service.revoke_content_analysis()
        revoked.set()

    revoker = threading.Thread(target=revoke)
    revoker.start()
    revoked_before_release = revoked.wait(0.2)
    release.set()
    scheduling.join(2)
    revoker.join(2)

    assert revoked_before_release is False
    assert revoked.is_set()
    assert service.pending_model_jobs() == []
    assert probes == ["mac-mini"]
    assert scheduler.enqueue_due() == {}
    assert probes == ["mac-mini"]


def test_revoke_waits_for_leased_worker_fetch_and_prevents_future_reads(
    db_path: Path, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("""[advisory_content]
enabled = true
backend_policy = "local"
external_consent = false
targets = ["/allowed/global-agents"]
allowed_roots = ["/allowed"]
max_file_bytes = 1024
max_bundle_bytes = 4096
excerpt_max_chars = 320
""")
    enqueue_advisory_check(
        db_path,
        analyzer_id="model.configuration",
        target_id="mac-mini",
        source_version="bundle-v1",
    )
    entered = threading.Event()
    release = threading.Event()
    revoked = threading.Event()
    fetches: list[str] = []
    bundle = _content_bundle("No issue.")

    def fetch(host_id, _target_ids):
        fetches.append(host_id)
        entered.set()
        assert release.wait(2)
        return bundle

    class Backend:
        def complete(self, _system: str, _user: str) -> str:
            return '{"findings":[]}'

    worker = ContentAnalysisWorker(
        consent_reader=lambda: load_config(config_path).advisory_content,
        bundle_fetcher=fetch,
        backend_factory=lambda _config: Backend(),
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
    )
    working = threading.Thread(target=worker.run_once)
    working.start()
    assert entered.wait(2)

    service = InsightsService(db_path, config_path=config_path)

    def revoke() -> None:
        service.revoke_content_analysis()
        revoked.set()

    revoker = threading.Thread(target=revoke)
    revoker.start()
    revoked_before_release = revoked.wait(0.2)
    release.set()
    working.join(2)
    revoker.join(2)

    assert revoked_before_release is False
    assert revoked.is_set()
    assert service.pending_model_jobs() == []
    assert fetches == ["mac-mini"]
    assert worker.run_once().skipped == 1
    assert fetches == ["mac-mini"]


def test_backend_result_returning_during_revoke_is_not_persisted(
    db_path: Path, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("""[advisory_content]
enabled = true
backend_policy = "local"
external_consent = false
targets = ["/allowed/global-agents"]
allowed_roots = ["/allowed"]
max_file_bytes = 1024
max_bundle_bytes = 4096
excerpt_max_chars = 320
""")
    bundle = _content_bundle(
        "Always inspect the repository. Always inspect the repository."
    )
    job = enqueue_advisory_check(
        db_path,
        analyzer_id="model.configuration",
        target_id="mac-mini",
        source_version=bundle.bundle_hash,
    )
    backend_entered = threading.Event()
    backend_release = threading.Event()
    revoked = threading.Event()

    class Backend:
        def complete(self, _system: str, _user: str) -> str:
            backend_entered.set()
            assert backend_release.wait(2)
            return json.dumps(
                {
                    "findings": [
                        {
                            "rule_id": "prompt.repetition",
                            "target_id": "global-agents",
                            "severity": "medium",
                            "confidence": "likely",
                            "title": "Repeated instruction",
                            "impact": "Repeated text consumes context.",
                            "evidence_excerpt": "Always inspect the repository.",
                            "remediation": ["Keep one copy of the instruction."],
                        }
                    ]
                }
            )

    worker = ContentAnalysisWorker(
        consent_reader=lambda: load_config(config_path).advisory_content,
        bundle_fetcher=lambda _host, _targets: bundle,
        backend_factory=lambda _config: Backend(),
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
    )
    working = threading.Thread(target=worker.run_once)
    working.start()
    assert backend_entered.wait(2)

    service = InsightsService(db_path, config_path=config_path)

    def revoke() -> None:
        service.revoke_content_analysis()
        revoked.set()

    revoker = threading.Thread(target=revoke)
    revoker.start()
    for _ in range(100):
        if not load_config(config_path).advisory_content.enabled:
            break
        threading.Event().wait(0.01)
    assert load_config(config_path).advisory_content.enabled is False
    assert revoked.is_set() is False
    backend_release.set()
    working.join(2)
    revoker.join(2)

    assert working.is_alive() is False
    assert revoker.is_alive() is False
    assert revoked.is_set()
    with duckdb.connect(str(db_path), read_only=True) as con:
        status, artifact_id = con.execute(
            "SELECT status, latest_artifact_id FROM pipeline_jobs WHERE job_id = ?",
            [job.job_id],
        ).fetchone()
        finding_count = con.execute(
            "SELECT count(*) FROM advisory_findings"
        ).fetchone()[0]
        occurrence_count = con.execute(
            "SELECT count(*) FROM advisory_occurrences"
        ).fetchone()[0]
        artifact_count = con.execute(
            "SELECT count(*) FROM pipeline_artifacts WHERE job_id = ?",
            [job.job_id],
        ).fetchone()[0]
    assert status == "cancelled"
    assert artifact_id is None
    assert finding_count == 0
    assert occurrence_count == 0
    assert artifact_count == 0


def test_reconsent_same_settings_replaces_cancelled_job_in_same_bucket(
    db_path: Path, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("""[advisory_content]
enabled = true
backend_policy = "local"
external_consent = false
targets = ["/allowed/global-agents"]
allowed_roots = ["/allowed"]
max_file_bytes = 1024
max_bundle_bytes = 4096
excerpt_max_chars = 320
""")
    bundle = _content_bundle("No issue.")
    probes: list[str] = []

    class Registry:
        def list_hosts(self):
            return [type("Host", (), {"host_id": "mac-mini"})()]

    scheduler = ContentAnalysisScheduler(
        duckdb_path=db_path,
        registry=Registry(),
        consent_reader=lambda: load_config(config_path).advisory_content,
        version_fetcher=lambda host_id, _targets: (
            probes.append(host_id),
            bundle.bundle_hash,
        )[1],
        interval_seconds=300,
        clock=lambda: 1_000,
    )
    assert set(scheduler.enqueue_due()) == {"mac-mini"}
    with duckdb.connect(str(db_path), read_only=True) as con:
        first_job_id = con.execute(
            "SELECT job_id FROM pipeline_jobs WHERE status = 'pending'"
        ).fetchone()[0]

    service = InsightsService(db_path, config_path=config_path)
    service.revoke_content_analysis()
    service.consent_content_analysis(
        backend="local", external_disclosure_accepted=False
    )

    assert set(scheduler.enqueue_due()) == {"mac-mini"}
    assert scheduler.enqueue_due() == {}
    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = con.execute("SELECT job_id, status FROM pipeline_jobs").fetchall()
    states = dict(rows)
    replacement_ids = [job_id for job_id, status in rows if status == "pending"]
    assert states[first_job_id] == "cancelled"
    assert len(replacement_ids) == 1
    assert replacement_ids[0] != first_job_id
    assert probes == ["mac-mini", "mac-mini", "mac-mini"]


def test_revocation_after_lease_requeues_job_and_resumes_after_enable(
    db_path: Path,
) -> None:
    current = [_content_config()]
    bundle = _content_bundle("No issue.")
    job = enqueue_advisory_check(
        db_path,
        analyzer_id="model.configuration",
        target_id="mac-mini",
        source_version=bundle.bundle_hash,
    )

    def fetch(_host, _targets):
        current[0] = _content_config(enabled=False)
        return bundle

    class Backend:
        def complete(self, _system: str, _user: str) -> str:
            return '{"findings":[]}'

    worker = ContentAnalysisWorker(
        consent_reader=lambda: current[0],
        bundle_fetcher=fetch,
        backend_factory=lambda _config: Backend(),
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
    )

    assert worker.run_once().skipped == 1
    with duckdb.connect(str(db_path), read_only=True) as con:
        assert (
            con.execute(
                "SELECT status FROM pipeline_jobs WHERE job_id = ?", [job.job_id]
            ).fetchone()[0]
            == "pending"
        )

    current[0] = _content_config()
    worker.bundle_fetcher = lambda _host, _targets: bundle
    assert worker.run_once().succeeded == 1


def test_server_factory_wires_collector_backend_and_durable_content_job(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _content_bundle("No issue here.")
    config = replace(
        default_config(),
        duckdb_path=db_path,
        advisory_content=_content_config(enabled=False),
    )
    current = [config.advisory_content]
    HarnessRegistry(db_path).register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="mac",
        local_url="http://127.0.0.1:7081",
        status="online",
    )
    probes: list[str] = []
    fetches: list[str] = []

    class Collector:
        def fetch_advisory_content_version(self, host_id, target_ids):
            probes.append(host_id)
            assert host_id == "mac-mini"
            assert target_ids == ["global-agents"]
            return {
                "bundle_hash": bundle.bundle_hash,
                "targets": [
                    {
                        "target_id": item.target_id,
                        "content_hash": item.content_hash,
                    }
                    for item in bundle.targets
                ],
            }

        def fetch_advisory_content_bundle(self, host_id, target_ids):
            fetches.append(host_id)
            assert host_id == "mac-mini"
            assert target_ids == ["global-agents"]
            return {
                "bundle_hash": bundle.bundle_hash,
                "created_at": bundle.created_at.isoformat(),
                "targets": [
                    {
                        "target_id": item.target_id,
                        "content_hash": item.content_hash,
                        "redacted_content": item.redacted_content,
                    }
                    for item in bundle.targets
                ],
            }

    class Backend:
        def complete(self, _system: str, _user: str) -> str:
            return '{"findings":[]}'

    monkeypatch.setattr(
        "drover.server.__main__.build_configured_analysis_backend",
        lambda **_kwargs: Backend(),
    )
    worker = _create_content_analysis_worker(
        cfg=config,
        metrics_collector=Collector(),
        backend_config=object(),
        consent_reader=lambda: current[0],
    )

    assert worker.run_once().skipped == 1
    assert probes == []
    assert fetches == []
    current[0] = _content_config()
    assert worker.run_once().succeeded == 1
    assert probes == ["mac-mini"]
    assert fetches == ["mac-mini"]
    assert worker.run_once().skipped == 1
    assert probes == ["mac-mini", "mac-mini"]
    assert fetches == ["mac-mini"]


def test_content_scheduler_coalesces_startup_and_enqueues_periodic_versions(
    db_path: Path,
) -> None:
    HarnessRegistry(db_path).register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="mac",
        local_url="http://127.0.0.1:7081",
        status="online",
    )
    now = [0.0]
    probes: list[str] = []
    bundle_hash = _content_bundle("No issue.").bundle_hash
    scheduler = ContentAnalysisScheduler(
        duckdb_path=db_path,
        registry=HarnessRegistry(db_path),
        consent_reader=lambda: _content_config(),
        version_fetcher=lambda host_id, _targets: (
            probes.append(host_id),
            bundle_hash,
        )[1],
        interval_seconds=3600,
        clock=lambda: now[0],
    )

    assert set(scheduler.enqueue_due()) == {"mac-mini"}
    assert scheduler.enqueue_due() == {}
    now[0] = 3600
    assert set(scheduler.enqueue_due()) == {"mac-mini"}
    assert probes == ["mac-mini", "mac-mini", "mac-mini"]
    with duckdb.connect(str(db_path), read_only=True) as con:
        assert (
            con.execute(
                "SELECT count(*) FROM pipeline_receipts WHERE source_key = ?",
                ["model.configuration:mac-mini"],
            ).fetchone()[0]
            == 2
        )


def test_content_scheduler_isolates_host_probe_failures_and_detects_changes(
    db_path: Path,
) -> None:
    registry = HarnessRegistry(db_path)
    for host_id in ("a-offline", "b-healthy"):
        registry.register_host(
            host_id=host_id,
            display_name=host_id,
            kind="mac",
            local_url=f"http://{host_id}:7081",
            status="online",
        )
    versions = {"b-healthy": "a" * 64}
    probes: list[str] = []

    def probe(host_id, _target_ids):
        probes.append(host_id)
        if host_id == "a-offline":
            raise RuntimeError("host is unreachable")
        return versions[host_id]

    scheduler = ContentAnalysisScheduler(
        duckdb_path=db_path,
        registry=registry,
        consent_reader=lambda: _content_config(),
        version_fetcher=probe,
        interval_seconds=3600,
        clock=lambda: 100,
    )

    first = scheduler.enqueue_due()
    unchanged = scheduler.enqueue_due()
    versions["b-healthy"] = "b" * 64
    changed = scheduler.enqueue_due()

    assert set(first) == {"b-healthy"}
    assert unchanged == {}
    assert set(changed) == {"b-healthy"}
    assert scheduler.last_failures == {"a-offline": "version_probe_failed"}
    assert probes == [
        "a-offline",
        "b-healthy",
        "a-offline",
        "b-healthy",
        "a-offline",
        "b-healthy",
    ]
    with duckdb.connect(str(db_path), read_only=True) as con:
        assert (
            con.execute(
                "SELECT count(*) FROM pipeline_receipts WHERE source_key = ?",
                ["model.configuration:b-healthy"],
            ).fetchone()[0]
            == 2
        )


def test_content_worker_fetches_one_bundle_only_after_each_claimed_attempt(
    db_path: Path,
) -> None:
    registry = HarnessRegistry(db_path)
    bundles = {
        "a-offline": _content_bundle("offline", host_id="a-offline"),
        "b-healthy": _content_bundle("healthy", host_id="b-healthy"),
    }
    for host_id in bundles:
        registry.register_host(
            host_id=host_id,
            display_name=host_id,
            kind="mac",
            local_url=f"http://{host_id}:7081",
            status="online",
        )
    probes: list[str] = []
    full_fetches: list[str] = []

    def probe(host_id, _target_ids):
        probes.append(host_id)
        return bundles[host_id].bundle_hash

    def fetch(host_id, _target_ids):
        full_fetches.append(host_id)
        if host_id == "a-offline":
            raise RuntimeError("host went offline after discovery")
        return bundles[host_id]

    scheduler = ContentAnalysisScheduler(
        duckdb_path=db_path,
        registry=registry,
        consent_reader=lambda: _content_config(),
        version_fetcher=probe,
        interval_seconds=3600,
        clock=lambda: 100,
    )
    worker = ContentAnalysisWorker(
        consent_reader=lambda: _content_config(),
        bundle_fetcher=fetch,
        backend_factory=lambda _config: type(
            "Backend", (), {"complete": lambda self, _system, _user: '{"findings":[]}'}
        )(),
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
        retry_delay=timedelta(0),
        scheduler=scheduler,
    )

    assert worker.run_once().failed == 1
    assert worker.run_once().succeeded == 1

    assert probes == ["a-offline", "b-healthy", "a-offline", "b-healthy"]
    assert full_fetches == ["a-offline", "b-healthy"]


def test_content_scheduler_retries_changed_version_after_current_lease_finishes(
    db_path: Path,
) -> None:
    registry = HarnessRegistry(db_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="mac",
        local_url="http://mac-mini:7081",
        status="online",
    )
    current_hash = ["a" * 64]
    scheduler = ContentAnalysisScheduler(
        duckdb_path=db_path,
        registry=registry,
        consent_reader=lambda: _content_config(),
        version_fetcher=lambda _host, _targets: current_hash[0],
        interval_seconds=3600,
        clock=lambda: 100,
    )
    assert set(scheduler.enqueue_due()) == {"mac-mini"}
    with duckdb.connect(str(db_path)) as con:
        ledger = Ledger(con)
        job = ledger.latest_job(
            "analyze_advisory_target", "model.configuration:mac-mini"
        )
        assert job is not None
        ledger.lease_job(
            job.job_id,
            worker_id="busy-worker",
            lease_expires_at=NOW + timedelta(minutes=5),
        )

    current_hash[0] = "b" * 64
    assert scheduler.enqueue_due() == {}
    with duckdb.connect(str(db_path)) as con:
        Ledger(con).succeed_job(job.job_id)

    assert set(scheduler.enqueue_due()) == {"mac-mini"}


def test_content_worker_retries_when_bundle_changes_after_version_probe(
    db_path: Path,
) -> None:
    registry = HarnessRegistry(db_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="mac",
        local_url="http://mac-mini:7081",
        status="online",
    )
    old = _content_bundle("old")
    new = _content_bundle("new")
    probe_hash = [old.bundle_hash]
    fetches: list[str] = []
    scheduler = ContentAnalysisScheduler(
        duckdb_path=db_path,
        registry=registry,
        consent_reader=lambda: _content_config(),
        version_fetcher=lambda _host, _targets: probe_hash[0],
        interval_seconds=3600,
        clock=lambda: 100,
    )
    worker = ContentAnalysisWorker(
        consent_reader=lambda: _content_config(),
        bundle_fetcher=lambda _host, _targets: (fetches.append("fetch"), new)[1],
        backend_factory=lambda _config: type(
            "Backend", (), {"complete": lambda self, _system, _user: '{"findings":[]}'}
        )(),
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
        retry_delay=timedelta(0),
        scheduler=scheduler,
    )

    assert worker.run_once().failed == 1
    with duckdb.connect(str(db_path), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM pipeline_artifacts").fetchone()[0] == 0

    probe_hash[0] = new.bundle_hash
    assert worker.run_once().succeeded == 1
    assert fetches == ["fetch", "fetch"]


def test_content_job_rolls_back_findings_when_ledger_completion_fails(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "Always inspect the repository. Always inspect the repository."
    bundle = _content_bundle(content)
    enqueue_advisory_check(
        db_path,
        analyzer_id="model.configuration",
        target_id="mac-mini",
        source_version=bundle.bundle_hash,
    )

    class Backend:
        def complete(self, _system: str, _user: str) -> str:
            return json.dumps(
                {
                    "findings": [
                        {
                            "rule_id": "prompt.repetition",
                            "target_id": "global-agents",
                            "severity": "medium",
                            "confidence": "likely",
                            "title": "Repeated instruction",
                            "impact": "Repeated text consumes context.",
                            "evidence_excerpt": "Always inspect the repository.",
                            "remediation": ["Keep one copy of the instruction."],
                        }
                    ]
                }
            )

    def fail_completion(self, *args, **kwargs):
        raise RuntimeError("injected artifact failure")

    monkeypatch.setattr(Ledger, "succeed_job", fail_completion)
    worker = ContentAnalysisWorker(
        consent_reader=lambda: _content_config(),
        bundle_fetcher=lambda _host, _targets: bundle,
        backend_factory=lambda _config: Backend(),
        duckdb_path=db_path,
        repository=AdvisoryRepository(db_path),
    )

    assert worker.run_once().failed == 1
    assert AdvisoryRepository(db_path).list_findings() == []


def test_clean_model_bundle_resolves_only_covered_nondismissed_targets(
    db_path: Path,
) -> None:
    repository = AdvisoryRepository(db_path)
    covered = repository.observe(
        _model_candidate("global-agents"), run_id="old-covered"
    )
    dismissed = repository.observe(
        _model_candidate("global-agents", rule_id="prompt.inefficiency"),
        run_id="old-dismissed",
    )
    repository.dismiss(dismissed.finding_id, reason="accepted tradeoff")
    unrequested = repository.observe(
        _model_candidate("project-agents"), run_id="old-unrequested"
    )
    bundle = _content_bundle("No issue remains.")
    enqueue_advisory_check(
        db_path,
        analyzer_id="model.configuration",
        target_id="mac-mini",
        source_version=bundle.bundle_hash,
    )
    worker = ContentAnalysisWorker(
        consent_reader=lambda: _content_config(),
        bundle_fetcher=lambda _host, _targets: bundle,
        backend_factory=lambda _config: type(
            "Backend", (), {"complete": lambda self, _system, _user: '{"findings":[]}'}
        )(),
        duckdb_path=db_path,
        repository=repository,
    )

    assert worker.run_once().succeeded == 1

    assert repository.get_finding(covered.finding_id).state.value == "resolved"
    assert repository.get_finding(dismissed.finding_id).state.value == "dismissed"
    assert repository.get_finding(unrequested.finding_id).state.value == "open"
    with duckdb.connect(str(db_path), read_only=True) as con:
        passing = con.execute(
            "SELECT outcome FROM advisory_occurrences WHERE finding_id = ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            [covered.finding_id],
        ).fetchone()
    assert passing == ("passing",)


def test_periodic_scheduler_uses_one_deterministic_version_per_interval(
    db_path: Path,
) -> None:
    now = [7200.0]
    scheduler = AdvisoryScheduler(
        duckdb_path=db_path,
        analyzer_ids=("connectors", "hooks"),
        full_review_interval_seconds=3600.0,
        clock=lambda: now[0],
    )

    first = scheduler.enqueue_due_full_review()
    same_window = scheduler.enqueue_due_full_review()
    now[0] = 10_800.0
    next_window = scheduler.enqueue_due_full_review()

    assert len(first) == 2
    assert same_window == []
    assert len(next_window) == 2
    assert {job.subject_key for job in next_window} == {
        "connectors:fleet",
        "hooks:fleet",
    }


def test_failed_periodic_enqueue_retries_same_interval(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from drover.server.advisory import jobs as advisory_jobs

    original = advisory_jobs.enqueue_advisory_check
    calls = 0

    def _flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database lock")
        return original(*args, **kwargs)

    monkeypatch.setattr(advisory_jobs, "enqueue_advisory_check", _flaky)
    scheduler = AdvisoryScheduler(
        duckdb_path=db_path,
        analyzer_ids=("connectors",),
        full_review_interval_seconds=3600,
        clock=lambda: 7200,
    )

    with pytest.raises(RuntimeError, match="database lock"):
        scheduler.enqueue_due_full_review()

    assert len(scheduler.enqueue_due_full_review()) == 1


def test_runtime_snapshot_reads_provider_health_without_credentials(
    db_path: Path,
) -> None:
    with duckdb.connect(str(db_path)) as con:
        con.execute(
            """
            INSERT INTO provider_connections (
              provider, account_label, host_id, enabled, error_category,
              credential_reference, last_attempt_at, updated_at
            ) VALUES ('openai', 'personal', 'mac-mini', TRUE, 'auth',
                      'keychain://secret', ?, ?)
            """,
            [NOW, NOW],
        )

    snapshot = load_operational_snapshot(
        db_path,
        "deterministic.connector_freshness",
        "fleet",
        "scheduled:2",
        analyzed_at=NOW,
    )

    assert snapshot.source_version == "scheduled:2"
    assert len(snapshot.provider_connections) == 1
    assert snapshot.provider_connections[0].status == "error"
    assert "keychain" not in repr(snapshot)


def test_runtime_registers_only_analyzers_with_snapshot_evidence() -> None:
    assert [item.analyzer_id for item in operational_analyzers()] == [
        "deterministic.connector_freshness",
        "deterministic.provider_reset_windows",
        "deterministic.telemetry_coverage",
        "deterministic.routing_mismatch",
        "deterministic.cache_read_efficiency",
        "deterministic.hook_validity",
    ]


def test_runtime_snapshot_populates_bounded_normalized_facts_without_content(
    db_path: Path,
) -> None:
    with duckdb.connect(str(db_path)) as con:
        con.execute("DROP VIEW spans_enriched")
        con.execute("""
            CREATE TABLE spans_enriched (
              span_id VARCHAR, session_id VARCHAR, start_time TIMESTAMPTZ,
              llm_provider VARCHAR,
              routing_provider VARCHAR, routing_model VARCHAR,
              prompt_tokens BIGINT, total_tokens BIGINT,
              cache_read_tokens BIGINT, cost_usd DOUBLE
            )
            """)
        _control_plane_execute(
            db_path,
            """
            INSERT INTO harness_sessions (
              session_id, host_id, harness, repo_owner, repo_name, command,
              status, model, started_at, updated_at
            ) VALUES
              ('s1', 'mac-mini', 'codex', 'acme', 'drover', 'codex', 'completed',
               'gpt-5', ?, ?),
              ('s2', 'mac-mini', 'codex', NULL, NULL, 'codex', 'completed',
               'gpt-5', ?, ?)
            """,
            [NOW, NOW, NOW, NOW],
        )
        con.execute(
            """
            INSERT INTO spans_enriched VALUES
              ('span-1', 's1', ?, 'openai', 'openai', 'gpt-4', 20000, 21000, 0, 1.5)
            """,
            [NOW],
        )
        descriptor = {
            "advisory": {
                "hooks": [
                    {
                        "hook_id": "session-start",
                        "harness_id": "codex",
                        "canonical_config_path": "/Users/operator/.codex/config.toml",
                        "canonical_executable_path": "/opt/drover/bin/drover-hook",
                        "enabled": True,
                        "executable_exists": False,
                        "executable_is_file": False,
                        "executable_is_executable": False,
                        "target_hash": "sha256:missing",
                        "allowlisted": True,
                    }
                ]
            },
            "secret": "do-not-copy",
            "config_content": "PRIVATE PROMPT",
        }
        _control_plane_execute(
            db_path,
            """
            INSERT INTO harness_hosts (
              host_id, display_name, kind, status, capabilities_json,
              last_seen_at, updated_at
            ) VALUES ('mac-mini', 'Mac Mini', 'local', 'online', ?, ?, ?)
            """,
            [json.dumps(descriptor), NOW, NOW],
        )

    telemetry = load_operational_snapshot(
        db_path,
        "deterministic.telemetry_coverage",
        "fleet",
        "facts:v1",
        analyzed_at=NOW,
    )
    routing = load_operational_snapshot(
        db_path, "deterministic.routing_mismatch", "fleet", "facts:v1", analyzed_at=NOW
    )
    hooks = load_operational_snapshot(
        db_path, "deterministic.hook_validity", "fleet", "facts:v1", analyzed_at=NOW
    )

    assert len(telemetry.telemetry) == 1
    assert telemetry.telemetry[0].total_sessions == 2
    assert telemetry.telemetry[0].repository_attributed_sessions == 1
    assert telemetry.telemetry[0].token_observed_sessions == 1
    assert len(routing.routing) == 1
    assert (routing.routing[0].decision_count, routing.routing[0].mismatch_count) == (
        1,
        1,
    )
    assert len(hooks.hooks) == 1
    assert hooks.hooks[0].target_hash == "sha256:missing"
    assert "PRIVATE PROMPT" not in repr(hooks)
    assert "do-not-copy" not in repr(hooks)

    findings = {
        analyzer.analyzer_id: analyzer.analyze(
            load_operational_snapshot(
                db_path, analyzer.analyzer_id, "fleet", "v1", analyzed_at=NOW
            )
        )
        for analyzer in operational_analyzers()
    }
    assert findings["deterministic.telemetry_coverage"]
    assert findings["deterministic.cache_read_efficiency"]
    assert findings["deterministic.hook_validity"]


def test_operational_fact_hash_coalesces_unchanged_rows(db_path: Path) -> None:
    first = operational_snapshot_source_version(
        db_path, "deterministic.telemetry_coverage", "fleet"
    )
    second = operational_snapshot_source_version(
        db_path, "deterministic.telemetry_coverage", "fleet"
    )

    assert first == second
    assert first.startswith("operational-facts:")


def test_runtime_telemetry_snapshot_caps_input_sessions(db_path: Path) -> None:
    with duckdb.connect(str(db_path)) as con:
        con.execute("DROP VIEW spans_enriched")
        con.execute("""
            CREATE TABLE spans_enriched (
              span_id VARCHAR, session_id VARCHAR, start_time TIMESTAMPTZ,
              prompt_tokens BIGINT, total_tokens BIGINT,
              cache_read_tokens BIGINT, cost_usd DOUBLE
            )
            """)
        _control_plane_execute(
            db_path,
            """
            INSERT INTO harness_sessions (
              session_id, host_id, harness, command, status, started_at, updated_at
            )
            SELECT 'session-' || i, 'mac-mini', 'codex', 'codex', 'completed',
                   ? - i * INTERVAL 1 SECOND, ? - i * INTERVAL 1 SECOND
            FROM range(513) rows(i)
            """,
            [NOW, NOW],
        )
    snapshot = load_operational_snapshot(
        db_path,
        "deterministic.telemetry_coverage",
        "fleet",
        "facts:v1",
        analyzed_at=NOW,
    )

    assert snapshot.telemetry[0].total_sessions == 512
    assert snapshot.telemetry[0].facts_complete is False


def test_runtime_snapshot_bounds_sessions_inside_the_seven_day_window(
    db_path: Path,
) -> None:
    with duckdb.connect(str(db_path)) as con:
        con.execute("DROP VIEW spans_enriched")
        con.execute("""
            CREATE TABLE spans_enriched (
              span_id VARCHAR, session_id VARCHAR, start_time TIMESTAMPTZ,
              llm_provider VARCHAR, routing_provider VARCHAR,
              routing_model VARCHAR, prompt_tokens BIGINT,
              total_tokens BIGINT, cache_read_tokens BIGINT, cost_usd DOUBLE
            )
            """)
        _control_plane_execute(
            db_path,
            """
            INSERT INTO harness_sessions (
              session_id, host_id, harness, command, status, model,
              started_at, updated_at
            )
            SELECT 'old-' || i, 'mac-mini', 'codex', 'codex', 'completed',
                   'gpt-5', ? - INTERVAL 8 DAY - i * INTERVAL 1 SECOND,
                   ? - INTERVAL 8 DAY - i * INTERVAL 1 SECOND
            FROM range(600) rows(i)
            """,
            [NOW, NOW],
        )
        _control_plane_execute(
            db_path,
            """
            INSERT INTO harness_sessions (
              session_id, host_id, harness, command, status, model,
              started_at, updated_at
            ) VALUES ('recent', 'mac-mini', 'codex', 'codex', 'completed',
                      'gpt-5', ?, ?)
            """,
            [NOW, NOW],
        )
        con.execute(
            """
            INSERT INTO spans_enriched VALUES
              ('recent-span', 'recent', ?, 'openai', 'openai', 'gpt-4',
               100, 100, 20, 0.1)
            """,
            [NOW],
        )

    telemetry = load_operational_snapshot(
        db_path,
        "deterministic.telemetry_coverage",
        "fleet",
        "facts:v1",
        analyzed_at=NOW,
    )
    routing = load_operational_snapshot(
        db_path,
        "deterministic.routing_mismatch",
        "fleet",
        "facts:v1",
        analyzed_at=NOW,
    )

    assert telemetry.telemetry[0].total_sessions == 1
    assert telemetry.telemetry[0].facts_complete is True
    assert routing.routing[0].decision_count == 1
    assert routing.routing[0].facts_complete is True


def test_runtime_snapshot_uses_latest_session_activity_including_ended_at(
    db_path: Path,
) -> None:
    with duckdb.connect(str(db_path)) as con:
        con.execute("DROP VIEW spans_enriched")
        con.execute("""
            CREATE TABLE spans_enriched (
              span_id VARCHAR, session_id VARCHAR, start_time TIMESTAMPTZ,
              llm_provider VARCHAR, routing_provider VARCHAR,
              routing_model VARCHAR, prompt_tokens BIGINT,
              total_tokens BIGINT, cache_read_tokens BIGINT, cost_usd DOUBLE
            )
            """)
        _control_plane_execute(
            db_path,
            """
            INSERT INTO harness_sessions (
              session_id, host_id, harness, command, status, model,
              started_at, updated_at, ended_at
            ) VALUES
              ('recently-ended', 'mac-mini', 'codex', 'codex', 'completed',
               'gpt-5', ? - INTERVAL 10 DAY, ? - INTERVAL 8 DAY, ?),
              ('fully-old', 'mac-mini', 'codex', 'codex', 'completed',
               'gpt-5', ? - INTERVAL 10 DAY, ? - INTERVAL 9 DAY,
               ? - INTERVAL 8 DAY)
            """,
            [NOW, NOW, NOW, NOW, NOW, NOW],
        )
        con.execute(
            """
            INSERT INTO spans_enriched VALUES
              ('recent-span', 'recently-ended', ?, 'openai', 'openai', 'gpt-4',
               100, 100, 20, 0.1),
              ('old-span', 'fully-old', ? - INTERVAL 8 DAY, 'openai', 'openai',
               'gpt-4', 100, 100, 20, 0.1)
            """,
            [NOW, NOW],
        )

    telemetry = load_operational_snapshot(
        db_path,
        "deterministic.telemetry_coverage",
        "fleet",
        "facts:v1",
        analyzed_at=NOW,
    )
    routing = load_operational_snapshot(
        db_path,
        "deterministic.routing_mismatch",
        "fleet",
        "facts:v1",
        analyzed_at=NOW,
    )

    assert telemetry.telemetry[0].total_sessions == 1
    assert telemetry.telemetry[0].observed_at == NOW
    assert telemetry.telemetry[0].facts_complete is True
    assert routing.routing[0].decision_count == 1
    assert routing.routing[0].observed_at == NOW
    assert routing.routing[0].facts_complete is True


def test_runtime_snapshot_caps_latest_spans_per_selected_session(
    db_path: Path,
) -> None:
    with duckdb.connect(str(db_path)) as con:
        con.execute("DROP VIEW spans_enriched")
        con.execute("""
            CREATE TABLE spans_enriched (
              span_id VARCHAR, session_id VARCHAR, start_time TIMESTAMPTZ,
              llm_provider VARCHAR, routing_provider VARCHAR,
              routing_model VARCHAR, prompt_tokens BIGINT,
              total_tokens BIGINT, cache_read_tokens BIGINT, cost_usd DOUBLE
            )
            """)
        _control_plane_execute(
            db_path,
            """
            INSERT INTO harness_sessions (
              session_id, host_id, harness, command, status, model,
              started_at, updated_at
            ) VALUES ('selected', 'mac-mini', 'codex', 'codex', 'completed',
                      'gpt-5', ?, ?)
            """,
            [NOW, NOW],
        )
        con.execute(
            """
            INSERT INTO spans_enriched
            SELECT 'span-' || i, 'selected', ? - i * INTERVAL 1 SECOND,
                   'openai', 'openai', 'gpt-4', 1, 1, 0, 0.1
            FROM range(9000) rows(i)
            """,
            [NOW],
        )

    telemetry = load_operational_snapshot(
        db_path,
        "deterministic.telemetry_coverage",
        "fleet",
        "facts:v1",
        analyzed_at=NOW,
    )
    routing = load_operational_snapshot(
        db_path, "deterministic.routing_mismatch", "fleet", "facts:v1", analyzed_at=NOW
    )

    assert telemetry.telemetry[0].prompt_tokens == 64
    assert routing.routing[0].decision_count == 64
    assert telemetry.telemetry[0].input_span_records == 8192
    assert routing.routing[0].input_span_records == 8192
    assert telemetry.telemetry[0].facts_complete is False
    assert routing.routing[0].facts_complete is False
    assert operational_analyzers()[2].analyze(telemetry) == []
    assert operational_analyzers()[3].analyze(routing) == []


def test_span_lookback_is_measured_from_analyzed_at(db_path: Path) -> None:
    """The span window is anchored to analyzed_at, never to the wall clock.

    This is what makes a snapshot reproducible: the same store and the same
    analyzed_at must yield the same facts whenever the query is run. It also
    keeps the tests in this module honest -- they place their fixtures
    relative to NOW, so a window measured from the real clock would make every
    assertion here a function of how long ago NOW was, and they would all
    start failing on a date rather than on a change.
    """

    with duckdb.connect(str(db_path)) as con:
        con.execute("DROP VIEW spans_enriched")
        con.execute("""
            CREATE TABLE spans_enriched (
              span_id VARCHAR, session_id VARCHAR, start_time TIMESTAMPTZ,
              llm_provider VARCHAR, routing_provider VARCHAR,
              routing_model VARCHAR, prompt_tokens BIGINT,
              total_tokens BIGINT, cache_read_tokens BIGINT, cost_usd DOUBLE
            )
            """)
        _control_plane_execute(
            db_path,
            """
            INSERT INTO harness_sessions (
              session_id, host_id, harness, command, status, model,
              started_at, updated_at
            ) VALUES ('selected', 'mac-mini', 'codex', 'codex', 'completed',
                      'gpt-5', ?, ?)
            """,
            [NOW, NOW],
        )
        con.execute(
            """
            INSERT INTO spans_enriched VALUES
              ('span-1', 'selected', ?, 'openai', 'openai', 'gpt-4',
               100, 100, 20, 0.1)
            """,
            [NOW],
        )

    def records_at(analyzed_at: datetime) -> int:
        snapshot = load_operational_snapshot(
            db_path,
            "deterministic.telemetry_coverage",
            "fleet",
            "facts:v1",
            analyzed_at=analyzed_at,
        )
        return snapshot.telemetry[0].input_span_records if snapshot.telemetry else 0

    assert records_at(NOW) == 1
    # One second past the lookback and the same span is gone, whatever the
    # real date happens to be when this runs.
    assert records_at(NOW + OPERATIONAL_SPAN_LOOKBACK + timedelta(seconds=1)) == 0


def test_incomplete_reset_window_facts_cannot_resolve_existing_finding(
    db_path: Path,
) -> None:
    repository = AdvisoryRepository(db_path)
    existing = repository.observe(
        FindingCandidate(
            analyzer_id="deterministic.provider_reset_windows",
            rule_id="connector.contradictory_reset_window",
            target_type="provider_connector",
            target_id="mac-mini/openai/personal",
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.HIGH,
            confidence=Confidence.CONFIRMED,
            title="OpenAI reports a contradictory reset window",
            impact="The reset countdown cannot be trusted.",
            remediation=("Refresh the connector, then run Check Again.",),
            evidence=(
                FindingEvidence(
                    source_ref="provider_connections:mac-mini/openai/personal",
                    observed_at=NOW,
                    fields={"invalid_window_count": 1},
                ),
            ),
        ),
        run_id="previous-run",
    )
    enqueue_advisory_check(
        db_path,
        analyzer_id="deterministic.provider_reset_windows",
        target_id="fleet",
        source_version="truncated:v2",
    )
    incomplete = AnalysisSnapshot(
        source_version="truncated:v2",
        analyzed_at=NOW,
        provider_connections=(
            ProviderConnectionObservation(
                provider="openai",
                account_label="personal",
                host_id="mac-mini",
                enabled=True,
                status="ok",
                observed_at=NOW,
                last_attempt_at=NOW,
                last_success_at=NOW,
                error_category=None,
                reset_windows=(
                    ProviderResetWindow(
                        kind="primary",
                        starts_at=NOW,
                        resets_at=NOW + timedelta(hours=1),
                    ),
                ),
                reset_windows_complete=False,
                source_ref="provider_connections:mac-mini/openai/personal",
            ),
        ),
    )
    worker = AdvisoryWorker(
        duckdb_path=db_path,
        repository=repository,
        snapshot_factory=lambda _analyzer, _target, _version: incomplete,
    )

    assert worker.run_once([operational_analyzers()[1]]).succeeded == 1
    assert repository.get_finding(existing.finding_id).state.value == "open"


def test_truncated_span_facts_cannot_resolve_existing_cache_finding(
    db_path: Path,
) -> None:
    repository = AdvisoryRepository(db_path)
    existing = repository.observe(
        FindingCandidate(
            analyzer_id="deterministic.cache_read_efficiency",
            rule_id="telemetry.cache_read_inefficiency",
            target_type="telemetry_source",
            target_id="mac-mini/codex",
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.MEDIUM,
            confidence=Confidence.CONFIRMED,
            title="Cache-read efficiency is low",
            impact="Repeated input is not using cache reads.",
            remediation=("Inspect repeated context, then run Check Again.",),
            evidence=(
                FindingEvidence(
                    source_ref="normalized-telemetry:mac-mini/codex",
                    observed_at=NOW,
                    fields={"cache_read_percent": 0},
                ),
            ),
        ),
        run_id="previous-run",
    )
    enqueue_advisory_check(
        db_path,
        analyzer_id="deterministic.cache_read_efficiency",
        target_id="fleet",
        source_version="truncated-spans:v2",
    )
    aggregate = replace(
        _telemetry_snapshot("unused").telemetry[0],
        prompt_tokens=20_000,
        facts_complete=False,
    )
    incomplete = AnalysisSnapshot(
        source_version="truncated-spans:v2",
        analyzed_at=NOW,
        telemetry=(aggregate,),
    )
    worker = AdvisoryWorker(
        duckdb_path=db_path,
        repository=repository,
        snapshot_factory=lambda _analyzer, _target, _version: incomplete,
    )

    assert worker.run_once([operational_analyzers()[4]]).succeeded == 1
    assert repository.get_finding(existing.finding_id).state.value == "open"


def test_connector_material_hash_coalesces_heartbeats_but_tracks_staleness(
    db_path: Path,
) -> None:
    with duckdb.connect(str(db_path)) as con:
        con.execute(
            """
            INSERT INTO provider_connections (
              provider, account_label, host_id, enabled, last_attempt_at,
              last_success_at, updated_at
            ) VALUES ('openai', 'personal', 'mac-mini', TRUE, ?, ?, ?)
            """,
            [NOW, NOW, NOW],
        )
    first = operational_snapshot_source_version(
        db_path,
        "deterministic.connector_freshness",
        "fleet",
        analyzed_at=NOW + timedelta(minutes=5),
    )
    with duckdb.connect(str(db_path)) as con:
        con.execute(
            """
            UPDATE provider_connections
            SET last_attempt_at = ?, last_success_at = ?, updated_at = ?
            WHERE host_id = 'mac-mini'
            """,
            [NOW + timedelta(minutes=1)] * 3,
        )
    heartbeat = operational_snapshot_source_version(
        db_path,
        "deterministic.connector_freshness",
        "fleet",
        analyzed_at=NOW + timedelta(minutes=6),
    )
    stale = operational_snapshot_source_version(
        db_path,
        "deterministic.connector_freshness",
        "fleet",
        analyzed_at=NOW + timedelta(minutes=17),
    )
    with duckdb.connect(str(db_path)) as con:
        con.execute(
            "UPDATE provider_connections SET error_category = 'auth' "
            "WHERE host_id = 'mac-mini'"
        )
    error = operational_snapshot_source_version(
        db_path,
        "deterministic.connector_freshness",
        "fleet",
        analyzed_at=NOW + timedelta(minutes=17),
    )

    assert heartbeat == first
    assert stale != heartbeat
    assert error != stale


def test_reset_window_snapshot_marks_truncation_and_hash_tracks_window_change(
    db_path: Path, tmp_path: Path
) -> None:
    service = ProviderUsageService(db_path, tmp_path / "lake")
    host = type("Host", (), {"host_id": "mac-mini"})()

    def payload(snapshot_id: str, reset_hours: int, window_count: int = 1):
        return {
            "accounts": [
                {
                    "snapshot_id": snapshot_id,
                    "dedup_key": f"dedup-{snapshot_id}",
                    "provider": "openai",
                    "account_label": "personal",
                    "plan_label": "plus",
                    "status": "ok",
                    "observed_at": (
                        NOW
                        + timedelta(
                            minutes={"one": 1, "two": 2, "many": 3}[snapshot_id]
                        )
                    ).isoformat(),
                    "source": "codex-app-server",
                    "windows": [
                        {
                            "kind": f"window-{index:02d}",
                            "used_percent": 25,
                            "starts_at": NOW.isoformat(),
                            "resets_at": (
                                NOW + timedelta(hours=reset_hours + index)
                            ).isoformat(),
                        }
                        for index in range(window_count)
                    ],
                }
            ]
        }

    service.refresh_host(host, fetch=lambda _host: payload("one", 1))
    first = operational_snapshot_source_version(
        db_path, "deterministic.provider_reset_windows", "fleet"
    )
    service.refresh_host(host, fetch=lambda _host: payload("two", 2))
    changed = operational_snapshot_source_version(
        db_path, "deterministic.provider_reset_windows", "fleet"
    )
    service.refresh_host(host, fetch=lambda _host: payload("many", 1, 33))
    truncated = load_operational_snapshot(
        db_path,
        "deterministic.provider_reset_windows",
        "fleet",
        "facts:many",
        analyzed_at=NOW,
    )

    assert changed != first
    assert len(truncated.provider_connections[0].reset_windows) == 32
    assert truncated.provider_connections[0].reset_windows_complete is False
    assert operational_analyzers()[1].analyze(truncated) == []


def test_empty_production_provider_windows_cannot_resolve_reset_finding(
    db_path: Path, tmp_path: Path
) -> None:
    repository = AdvisoryRepository(db_path)
    existing = repository.observe(
        FindingCandidate(
            analyzer_id="deterministic.provider_reset_windows",
            rule_id="connector.contradictory_reset_window",
            target_type="provider_connector",
            target_id="mac-mini/openai/personal",
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.HIGH,
            confidence=Confidence.CONFIRMED,
            title="OpenAI reports a contradictory reset window",
            impact="The reset countdown cannot be trusted.",
            remediation=("Refresh the connector, then run Check Again.",),
            evidence=(
                FindingEvidence(
                    source_ref="provider_connections:mac-mini/openai/personal",
                    observed_at=NOW,
                    fields={"invalid_window_count": 1},
                ),
            ),
        ),
        run_id="previous-run",
    )
    snapshot = ProviderAccountSnapshot(
        snapshot_id="empty-windows",
        dedup_key="empty-windows-dedup",
        provider="openai",
        account_label="personal",
        plan_label="plus",
        host_id="mac-mini",
        status="ok",
        observed_at=NOW,
        windows=(),
        source="codex-app-server",
    )
    service = ProviderUsageService(db_path, tmp_path / "lake")
    service._persist_new_snapshots((snapshot,), host_id="mac-mini")
    service._record_snapshot_attempts((snapshot,), attempted_at=NOW)
    loaded = load_operational_snapshot(
        db_path,
        "deterministic.provider_reset_windows",
        "fleet",
        "empty:v2",
        analyzed_at=NOW,
    )
    enqueue_advisory_check(
        db_path,
        analyzer_id="deterministic.provider_reset_windows",
        target_id="fleet",
        source_version="empty:v2",
    )
    worker = AdvisoryWorker(
        duckdb_path=db_path,
        repository=repository,
        snapshot_factory=lambda _analyzer, _target, _version: loaded,
    )

    assert loaded.provider_connections[0].reset_windows == ()
    assert loaded.provider_connections[0].reset_windows_complete is False
    assert worker.run_once([operational_analyzers()[1]]).succeeded == 1
    assert repository.get_finding(existing.finding_id).state.value == "open"


def test_scheduler_uses_material_fact_versions_to_coalesce_unchanged_reviews(
    db_path: Path,
) -> None:
    now = [0.0]
    version = ["stable"]
    version_calls: list[tuple[str, str]] = []

    def source_version(analyzer_id: str, target_id: str) -> str:
        version_calls.append((analyzer_id, target_id))
        return f"facts:{analyzer_id}:{target_id}:{version[0]}"

    scheduler = AdvisoryScheduler(
        duckdb_path=db_path,
        analyzer_ids=("deterministic.telemetry_coverage",),
        full_review_interval_seconds=60,
        clock=lambda: now[0],
        source_version_factory=source_version,
    )

    first = scheduler.enqueue_due_full_review()[0]
    assert scheduler.enqueue_due_full_review() == []
    version[0] = "stale-threshold-crossed"
    assert scheduler.enqueue_due_full_review() == []
    assert version_calls == [("deterministic.telemetry_coverage", "fleet")]
    now[0] = 60.0
    changed_next_bucket = scheduler.enqueue_due_full_review()

    assert changed_next_bucket[0].job_id == first.job_id
    assert version_calls == [
        ("deterministic.telemetry_coverage", "fleet"),
        ("deterministic.telemetry_coverage", "fleet"),
    ]
    with duckdb.connect(str(db_path), read_only=True) as con:
        assert (
            con.execute(
                "SELECT count(*) FROM pipeline_receipts WHERE source_key = ?",
                ["deterministic.telemetry_coverage:fleet"],
            ).fetchone()[0]
            == 2
        )


def test_failed_material_version_pass_retries_in_the_same_interval(
    db_path: Path,
) -> None:
    calls = 0

    def source_version(analyzer_id: str, target_id: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary snapshot failure")
        return f"facts:{analyzer_id}:{target_id}:stable"

    scheduler = AdvisoryScheduler(
        duckdb_path=db_path,
        analyzer_ids=("deterministic.telemetry_coverage",),
        full_review_interval_seconds=60,
        clock=lambda: 0.0,
        source_version_factory=source_version,
    )

    with pytest.raises(RuntimeError, match="temporary snapshot failure"):
        scheduler.enqueue_due_full_review()

    assert len(scheduler.enqueue_due_full_review()) == 1
    assert calls == 2


def test_runtime_snapshot_includes_latest_provider_reset_windows(
    db_path: Path, tmp_path: Path
) -> None:
    service = ProviderUsageService(db_path, tmp_path / "lake")
    payload = {
        "accounts": [
            {
                "snapshot_id": "snapshot-1",
                "dedup_key": "dedup-1",
                "provider": "openai",
                "account_label": "personal",
                "plan_label": "plus",
                "status": "ok",
                "observed_at": NOW.isoformat(),
                "source": "codex-app-server",
                "windows": [
                    {
                        "kind": "primary",
                        "used_percent": 25,
                        "starts_at": NOW.isoformat(),
                        "resets_at": (NOW + timedelta(hours=5)).isoformat(),
                    }
                ],
            }
        ]
    }
    host = type("Host", (), {"host_id": "mac-mini"})()
    service.refresh_host(host, fetch=lambda _host: payload)

    snapshot = load_operational_snapshot(
        db_path,
        "deterministic.provider_reset_windows",
        "fleet",
        "scheduled:2",
        analyzed_at=NOW,
    )

    assert len(snapshot.provider_connections) == 1
    assert [
        (item.kind, item.resets_at)
        for item in snapshot.provider_connections[0].reset_windows
    ] == [("primary", NOW + timedelta(hours=5))]


def test_full_review_interval_is_runtime_configurable(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[advisory]\nfull_review_interval_seconds = 7200\n")

    config = load_config(config_path)

    assert config.advisory_full_review_interval_seconds == 7200.0


def test_provider_refresh_notifies_only_for_material_operational_change() -> None:
    notifications: list[tuple[str, str]] = []
    now = [0.0]

    class _Registry:
        def list_hosts(self):
            # A real HarnessHost: the refresh loop calls is_stale() on whatever
            # the registry yields. last_seen_at=None means "never reported",
            # which is not staleness, so this stays a pure notification test.
            return [
                HarnessHost(
                    host_id="mac-mini",
                    display_name="Mac Mini",
                    kind="macos",
                    status="online",
                    connection_kind="direct",
                )
            ]

    class _ProviderUsage:
        def refresh_host(self, host):
            return ()

    loop = ProviderRefreshLoop(
        provider_usage=_ProviderUsage(),
        registry=_Registry(),
        shutdown_event=__import__("threading").Event(),
        interval_seconds=300,
        clock=lambda: now[0],
        operational_source_version=lambda _host_id: "provider-state:stable",
        on_operational_change=lambda host_id, version: notifications.append(
            (host_id, version)
        ),
    )

    loop.run_once()
    now[0] = 300.0
    loop.run_once()

    assert len(notifications) == 1
    assert notifications[0][0] == "mac-mini"
    assert notifications[0][1] == "provider-state:stable"


def test_provider_source_version_ignores_attempt_time_but_tracks_errors(
    db_path: Path, tmp_path: Path
) -> None:
    service = ProviderUsageService(db_path, tmp_path / "lake")
    with duckdb.connect(str(db_path)) as con:
        con.execute(
            """
            INSERT INTO provider_connections (
              provider, account_label, host_id, enabled, last_attempt_at,
              error_category
            ) VALUES ('openai', 'personal', 'mac-mini', TRUE, ?, NULL)
            """,
            [NOW],
        )
    first = service.operational_source_version("mac-mini")
    with duckdb.connect(str(db_path)) as con:
        con.execute(
            "UPDATE provider_connections SET last_attempt_at = ? WHERE host_id = ?",
            [NOW + timedelta(minutes=5), "mac-mini"],
        )
    unchanged = service.operational_source_version("mac-mini")
    with duckdb.connect(str(db_path)) as con:
        con.execute(
            "UPDATE provider_connections SET error_category = 'auth' WHERE host_id = ?",
            ["mac-mini"],
        )
    changed = service.operational_source_version("mac-mini")

    assert unchanged == first
    assert changed != first


def test_check_again_scopes_provider_finding_to_host_and_executes_current_facts(
    db_path: Path,
) -> None:
    with duckdb.connect(str(db_path)) as con:
        con.execute(
            """
            INSERT INTO provider_connections (
              provider, account_label, host_id, enabled, error_category,
              last_attempt_at, updated_at
            ) VALUES ('openai', 'personal', 'mac-mini', TRUE, 'auth', ?, ?)
            """,
            [NOW, NOW],
        )
    repository = AdvisoryRepository(db_path)
    finding = repository.observe(
        FindingCandidate(
            analyzer_id="deterministic.connector_freshness",
            rule_id="connector.stale",
            target_type="provider_connector",
            target_id="mac-mini/openai/personal",
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.HIGH,
            confidence=Confidence.CONFIRMED,
            title="OpenAI connector data is stale",
            impact="Provider capacity may be stale.",
            remediation=("Refresh the connector, then run Check Again.",),
            evidence=(
                FindingEvidence(
                    source_ref="provider_connections:mac-mini/openai/personal",
                    observed_at=NOW,
                    fields={"status": "stale"},
                ),
            ),
            content_hash="old-content-hash",
        ),
        run_id="old-analysis-run",
    )

    queued = InsightsService(db_path).check_again(finding.finding_id)
    worker = AdvisoryWorker(
        duckdb_path=db_path,
        repository=repository,
        snapshot_factory=lambda analyzer_id, target_id, version: load_operational_snapshot(
            db_path, analyzer_id, target_id, version, analyzed_at=NOW
        ),
    )
    result = worker.run_once([ConnectorFreshnessAnalyzer()])

    assert result.succeeded == 1
    with duckdb.connect(str(db_path), read_only=True) as con:
        subject_key, source_version, status = con.execute(
            """
            SELECT j.subject_key, r.source_version, j.status
            FROM pipeline_jobs j
            JOIN pipeline_receipts r ON r.receipt_id = j.caused_by_receipt_id
            WHERE j.job_id = ?
            """,
            [queued["job_id"]],
        ).fetchone()
    assert subject_key == "deterministic.connector_freshness:mac-mini"
    assert source_version.startswith("provider-state:")
    assert source_version != "old-content-hash"
    assert status == "succeeded"
    assert any(item.rule_id == "connector.error" for item in repository.list_findings())


def test_provider_insight_detail_advertises_scoped_check_again(db_path: Path) -> None:
    repository = AdvisoryRepository(db_path)
    finding = repository.observe(
        FindingCandidate(
            analyzer_id="deterministic.provider_reset_windows",
            rule_id="provider.reset.passed",
            target_type="provider_connector",
            target_id="mac-mini/openai/personal",
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.MEDIUM,
            confidence=Confidence.CONFIRMED,
            title="Reset window passed",
            impact="Capacity may need refreshing.",
            remediation=("Refresh the connector.",),
            evidence=(
                FindingEvidence(
                    source_ref="provider:mac-mini/openai/personal",
                    observed_at=NOW,
                    fields={"status": "stale"},
                ),
            ),
            content_hash="provider-old",
        ),
        run_id="run-old",
    )

    detail = InsightsService(db_path).get_insight(finding.finding_id)

    assert detail["actions"]["check_again"] == {"available": True, "reason": None}


def test_model_check_again_replays_latest_material_version_behind_consent(
    db_path: Path, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("""[advisory_content]
enabled = true
backend_policy = "local"
external_consent = false
targets = ["/allowed/global-agents"]
allowed_roots = ["/allowed"]
max_file_bytes = 1024
max_bundle_bytes = 4096
excerpt_max_chars = 320
""")
    repository = AdvisoryRepository(db_path)
    finding = repository.observe(
        FindingCandidate(
            analyzer_id="model.configuration",
            rule_id="prompt.repetition",
            target_type="configuration_target",
            target_id="mac-mini/global-agents",
            analyzer_class=AnalyzerClass.MODEL,
            severity=Severity.MEDIUM,
            confidence=Confidence.LIKELY,
            title="Repeated instruction",
            impact="Repeated text consumes context.",
            remediation=("Keep one copy.",),
            evidence=(
                FindingEvidence(
                    source_ref="content:mac-mini/global-agents#old",
                    observed_at=NOW,
                    fields={"bundle_hash": "old"},
                    excerpt="Repeated instruction",
                ),
            ),
            content_hash="old-target-hash",
        ),
        run_id="run-old",
    )
    existing = enqueue_advisory_check(
        db_path,
        analyzer_id="model.configuration",
        target_id="mac-mini",
        source_version="bundle-current",
    )
    with duckdb.connect(str(db_path)) as con:
        Ledger(con).lease_job(existing.job_id, worker_id="content-worker")
        Ledger(con).succeed_job(existing.job_id)

    service = InsightsService(db_path, config_path=config_path)
    detail = service.get_insight(finding.finding_id)
    queued = service.check_again(finding.finding_id)

    assert detail["actions"]["check_again"] == {"available": True, "reason": None}
    with duckdb.connect(str(db_path), read_only=True) as con:
        subject_key, source_version = con.execute(
            """
            SELECT j.subject_key, r.source_version
            FROM pipeline_jobs j
            JOIN pipeline_receipts r ON r.receipt_id = j.caused_by_receipt_id
            WHERE j.job_id = ?
            """,
            [queued["job_id"]],
        ).fetchone()
    assert subject_key == "model.configuration:mac-mini"
    assert source_version == "bundle-current"


def test_check_again_is_truthfully_unavailable_without_runtime_or_consent(
    db_path: Path, tmp_path: Path
) -> None:
    repository = AdvisoryRepository(db_path)
    unsupported = repository.observe(
        FindingCandidate(
            analyzer_id="deterministic.hook_validity",
            rule_id="hook.missing",
            target_type="hook",
            target_id="mac-mini/codex/pre-tool",
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.HIGH,
            confidence=Confidence.CONFIRMED,
            title="Hook is missing",
            impact="The hook cannot run.",
            remediation=("Restore the hook.",),
            evidence=(
                FindingEvidence(
                    source_ref="hook:mac-mini/codex/pre-tool",
                    observed_at=NOW,
                    fields={"exists": False},
                ),
            ),
            content_hash="hook-old",
        ),
        run_id="run-old",
    )
    model = repository.observe(
        FindingCandidate(
            analyzer_id="model.configuration",
            rule_id="prompt.repetition",
            target_type="configuration_target",
            target_id="mac-mini/global-agents",
            analyzer_class=AnalyzerClass.MODEL,
            severity=Severity.MEDIUM,
            confidence=Confidence.LIKELY,
            title="Repeated instruction",
            impact="Repeated text consumes context.",
            remediation=("Keep one copy.",),
            evidence=(
                FindingEvidence(
                    source_ref="content:mac-mini/global-agents#old",
                    observed_at=NOW,
                    fields={"bundle_hash": "old"},
                    excerpt="Repeated instruction",
                ),
            ),
            content_hash="old-target-hash",
        ),
        run_id="run-old",
    )
    service = InsightsService(db_path, config_path=tmp_path / "missing.toml")

    assert (
        service.get_insight(unsupported.finding_id)["actions"]["check_again"][
            "available"
        ]
        is False
    )
    assert service.get_insight(model.finding_id)["actions"]["check_again"] == {
        "available": False,
        "reason": "Enable content analysis before checking again.",
    }
    with pytest.raises(InvalidInsightTransition, match="unavailable"):
        service.check_again(unsupported.finding_id)
    with pytest.raises(InvalidInsightTransition, match="Enable content analysis"):
        service.check_again(model.finding_id)


def test_check_again_supports_scoped_runtime_analyzers_with_current_facts(
    db_path: Path,
) -> None:
    CURRENT = _current_moment()
    with duckdb.connect(str(db_path)) as con:
        con.execute("DROP VIEW spans_enriched")
        con.execute("""
            CREATE TABLE spans_enriched (
              span_id VARCHAR, session_id VARCHAR, start_time TIMESTAMPTZ,
              llm_provider VARCHAR, routing_provider VARCHAR,
              routing_model VARCHAR, prompt_tokens BIGINT,
              total_tokens BIGINT, cache_read_tokens BIGINT, cost_usd DOUBLE
            )
            """)
        _control_plane_execute(
            db_path,
            """
            INSERT INTO harness_sessions (
              session_id, host_id, harness, command, status, model,
              started_at, updated_at
            ) VALUES
              ('codex-session', 'mac-mini', 'codex', 'codex', 'completed',
               'gpt-5', ?, ?),
              ('claude-session', 'mac-mini', 'claude', 'claude', 'completed',
               'claude-4', ?, ?)
            """,
            [CURRENT, CURRENT, CURRENT, CURRENT],
        )
        con.execute(
            """
            INSERT INTO spans_enriched VALUES
              ('codex-span', 'codex-session', ?, 'openai', 'openai', 'gpt-4',
               100, 100, 0, 0.1),
              ('claude-span', 'claude-session', ?, 'anthropic', 'anthropic',
               'claude-3', 100, 100, 0, 0.1)
            """,
            [CURRENT, CURRENT],
        )
        con.execute(
            """
            INSERT INTO spans_enriched
            SELECT 'bulk-' || lpad(i::VARCHAR, 5, '0'), 'claude-session', ?,
                   'anthropic', 'anthropic', 'claude-3', 1, 1, 0, 0.01
            FROM range(8200) rows(i)
            """,
            [CURRENT],
        )
        descriptor = {
            "advisory": {
                "hooks": [
                    {
                        "hook_id": "pre-tool",
                        "harness_id": "codex",
                        "canonical_config_path": "/tmp/config",
                        "canonical_executable_path": "/tmp/hook",
                        "enabled": True,
                        "executable_exists": False,
                        "executable_is_file": False,
                        "executable_is_executable": False,
                        "target_hash": "sha256:missing",
                        "allowlisted": True,
                    },
                    {
                        "hook_id": "pre-tool",
                        "harness_id": "claude",
                        "canonical_config_path": "/tmp/other-config",
                        "canonical_executable_path": "/tmp/other-hook",
                        "enabled": True,
                        "executable_exists": False,
                        "executable_is_file": False,
                        "executable_is_executable": False,
                        "target_hash": "sha256:other",
                        "allowlisted": True,
                    },
                ]
            }
        }
        _control_plane_execute(
            db_path,
            """
            INSERT INTO harness_hosts (
              host_id, display_name, kind, status, capabilities_json,
              last_seen_at, updated_at
            ) VALUES ('mac-mini', 'Mac Mini', 'local', 'online', ?, ?, ?)
            """,
            [json.dumps(descriptor), CURRENT, CURRENT],
        )

    repository = AdvisoryRepository(db_path)
    candidates = (
        FindingCandidate(
            analyzer_id="deterministic.telemetry_coverage",
            rule_id="telemetry.span_coverage",
            target_type="telemetry_source",
            target_id="mac-mini/codex",
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.MEDIUM,
            confidence=Confidence.CONFIRMED,
            title="Telemetry coverage is low",
            impact="Metrics are incomplete.",
            remediation=("Repair telemetry, then run Check Again.",),
            evidence=(FindingEvidence("telemetry:codex", CURRENT, {"coverage": 0}),),
        ),
        FindingCandidate(
            analyzer_id="deterministic.cache_read_efficiency",
            rule_id="cache.read_efficiency",
            target_type="telemetry_source",
            target_id="mac-mini/codex",
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.LOW,
            confidence=Confidence.CONFIRMED,
            title="Cache efficiency is low",
            impact="Repeated prompts cost more.",
            remediation=("Review prompts, then run Check Again.",),
            evidence=(FindingEvidence("telemetry:codex", CURRENT, {"ratio": 0}),),
        ),
        FindingCandidate(
            analyzer_id="deterministic.routing_mismatch",
            rule_id="routing.mismatch_frequency",
            target_type="routing_policy",
            target_id="mac-mini/codex/openai",
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.MEDIUM,
            confidence=Confidence.CONFIRMED,
            title="Routing mismatch",
            impact="Requests use an unexpected model.",
            remediation=("Review routing, then run Check Again.",),
            evidence=(FindingEvidence("routing:codex", CURRENT, {"count": 1}),),
        ),
        FindingCandidate(
            analyzer_id="deterministic.hook_validity",
            rule_id="hook.missing",
            target_type="hook",
            target_id="mac-mini/codex/pre-tool",
            analyzer_class=AnalyzerClass.DETERMINISTIC,
            severity=Severity.HIGH,
            confidence=Confidence.CONFIRMED,
            title="Hook is missing",
            impact="The hook cannot run.",
            remediation=("Restore the hook, then run Check Again.",),
            evidence=(FindingEvidence("hook:codex", CURRENT, {"exists": False}),),
        ),
    )
    findings = [repository.observe(item, run_id="old") for item in candidates]
    service = InsightsService(db_path)

    queued = [service.check_again(item.finding_id) for item in findings]

    assert all(
        service.get_insight(item.finding_id)["actions"]["check_again"]
        == {"available": True, "reason": None}
        for item in findings
    )
    with duckdb.connect(str(db_path), read_only=True) as con:
        jobs = con.execute(
            """
            SELECT j.subject_key, r.source_version, j.status
            FROM pipeline_jobs j
            JOIN pipeline_receipts r ON r.receipt_id = j.caused_by_receipt_id
            WHERE j.job_id IN (?, ?, ?, ?)
            ORDER BY j.subject_key
            """,
            [item["job_id"] for item in queued],
        ).fetchall()
    assert [(subject, status) for subject, _, status in jobs] == [
        ("deterministic.cache_read_efficiency:mac-mini/codex", "pending"),
        ("deterministic.hook_validity:mac-mini/codex/pre-tool", "pending"),
        ("deterministic.routing_mismatch:mac-mini/codex/openai", "pending"),
        ("deterministic.telemetry_coverage:mac-mini/codex", "pending"),
    ]
    assert all(version.startswith("operational-facts:") for _, version, _ in jobs)
    scoped_telemetry = load_operational_snapshot(
        db_path,
        "deterministic.telemetry_coverage",
        "mac-mini/codex",
        "current",
        analyzed_at=CURRENT,
    )
    scoped_routing = load_operational_snapshot(
        db_path,
        "deterministic.routing_mismatch",
        "mac-mini/codex/openai",
        "current",
        analyzed_at=CURRENT,
    )
    scoped_hooks = load_operational_snapshot(
        db_path,
        "deterministic.hook_validity",
        "mac-mini/codex/pre-tool",
        "current",
        analyzed_at=CURRENT,
    )
    assert [item.target_id for item in scoped_telemetry.telemetry] == ["mac-mini/codex"]
    assert [item.target_id for item in scoped_routing.routing] == [
        "mac-mini/codex/openai"
    ]
    assert [
        f"{item.host_id}/{item.harness_id}/{item.hook_id}"
        for item in scoped_hooks.hooks
    ] == ["mac-mini/codex/pre-tool"]
