"""Durable scheduling and isolated execution for advisory analyzers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from drover.config import default_config, load_config
from drover.config import AdvisoryContentConfig
from drover.schema import bootstrap
from drover.server.advisory.analyzers import AnalysisSnapshot, TelemetryAggregate
from drover.server.advisory.analyzers.connectors import ConnectorFreshnessAnalyzer
from drover.server.advisory.jobs import (
    AdvisoryScheduler,
    enqueue_advisory_check,
    enqueue_operational_checks,
)
from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.service import InsightsService
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    FindingCandidate,
    FindingEvidence,
    Severity,
)
from drover.server.advisory.worker import (
    AdvisoryWorker,
    ContentAnalysisScheduler,
    ContentAnalysisWorker,
    load_operational_snapshot,
    operational_analyzers,
)
from drover.server.advisory.content_targets import BundledTarget, ContentBundle
from drover.server.cockpit.service import ProviderRefreshLoop
from drover.server.ledger import Ledger
from drover.server.providers.service import ProviderUsageService
from drover.server.__main__ import _create_content_analysis_worker
from drover.server.harness.registry import HarnessRegistry

NOW = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)


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
    fetches: list[str] = []

    class Collector:
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
    assert fetches == []
    current[0] = _content_config()
    assert worker.run_once().succeeded == 1
    assert fetches == ["mac-mini"]
    assert worker.run_once().skipped == 1
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
    fetches: list[str] = []
    scheduler = ContentAnalysisScheduler(
        duckdb_path=db_path,
        registry=HarnessRegistry(db_path),
        consent_reader=lambda: _content_config(),
        bundle_fetcher=lambda host_id, _targets: (
            fetches.append(host_id),
            _content_bundle("No issue."),
        )[1],
        interval_seconds=3600,
        clock=lambda: now[0],
    )

    assert set(scheduler.enqueue_due()) == {"mac-mini"}
    assert scheduler.enqueue_due() == {}
    now[0] = 3600
    assert set(scheduler.enqueue_due()) == {"mac-mini"}
    assert fetches == ["mac-mini", "mac-mini"]
    with duckdb.connect(str(db_path), read_only=True) as con:
        assert (
            con.execute(
                "SELECT count(*) FROM pipeline_receipts WHERE source_key = ?",
                ["model.configuration:mac-mini"],
            ).fetchone()[0]
            == 2
        )


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
        db_path, "deterministic.connector_freshness", "fleet", "scheduled:2"
    )

    assert snapshot.source_version == "scheduled:2"
    assert len(snapshot.provider_connections) == 1
    assert snapshot.provider_connections[0].status == "error"
    assert "keychain" not in repr(snapshot)


def test_runtime_registers_only_analyzers_with_snapshot_evidence() -> None:
    assert [item.analyzer_id for item in operational_analyzers()] == [
        "deterministic.connector_freshness",
        "deterministic.provider_reset_windows",
    ]


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
        db_path, "deterministic.provider_reset_windows", "fleet", "scheduled:2"
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
        def list_hosts(self, *, status: str):
            return [type("Host", (), {"host_id": "mac-mini"})()]

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
            db_path, analyzer_id, target_id, version
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
