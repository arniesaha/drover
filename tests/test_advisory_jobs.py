"""Durable scheduling and isolated execution for advisory analyzers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import duckdb
import pytest

from drover.config import load_config
from drover.schema import bootstrap
from drover.server.advisory.analyzers import AnalysisSnapshot, TelemetryAggregate
from drover.server.advisory.jobs import (
    AdvisoryScheduler,
    enqueue_advisory_check,
    enqueue_operational_checks,
)
from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    FindingCandidate,
    FindingEvidence,
    Severity,
)
from drover.server.advisory.worker import (
    AdvisoryWorker,
    load_operational_snapshot,
    operational_analyzers,
)
from drover.server.cockpit.service import ProviderRefreshLoop
from drover.server.ledger import Ledger
from drover.server.providers.service import ProviderUsageService

NOW = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)


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
