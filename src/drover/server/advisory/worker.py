"""Isolated advisory analyzer execution over the durable pipeline ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Protocol

from drover.config import AdvisoryContentConfig

from drover.server.advisory.analyzers import (
    MAX_SNAPSHOT_RECORDS,
    AnalysisSnapshot,
    Analyzer,
    ProviderConnectionObservation,
    ProviderResetWindow,
)
from drover.server.advisory.analyzers.connectors import (
    ConnectorFreshnessAnalyzer,
    ProviderResetWindowAnalyzer,
)
from drover.server.advisory.jobs import (
    ADVISORY_ARTIFACT_KIND,
    ADVISORY_JOB_KIND,
    AdvisoryScheduler,
    enqueue_advisory_check,
)
from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.content_targets import (
    ContentBundle,
    validate_content_bundle,
)
from drover.server.advisory.model_analyzer import (
    AnalysisBackend,
    AnalysisConsentRevoked,
    MODEL_ANALYZER_ID,
    ModelConfigurationAnalyzer,
    ModelFindingError,
)
from drover.server.advisory.types import FindingCandidate, FindingState
from drover.server.db import open_duckdb_connection
from drover.server.ledger import ArtifactSpec, Job, Ledger

log = logging.getLogger("drover.advisory")
SnapshotFactory = Callable[[str, str, str], AnalysisSnapshot]


@dataclass(frozen=True)
class AdvisoryRunResult:
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class ContentAnalysisResult:
    """Content-free result safe to attach to attempt metrics or artifacts."""

    status: str
    artifact: Mapping[str, Any]


class ContentBundleFetcher(Protocol):
    def __call__(self, host_id: str, target_ids: tuple[str, ...]) -> ContentBundle: ...


class ContentAnalysisScheduler:
    """Discover consented bundle versions and enqueue coalesced model jobs."""

    def __init__(
        self,
        *,
        duckdb_path: str | Path,
        registry,
        consent_reader: Callable[[], AdvisoryContentConfig],
        bundle_fetcher: ContentBundleFetcher,
        interval_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("content review interval must be positive")
        self.duckdb_path = Path(duckdb_path)
        self.registry = registry
        self.consent_reader = consent_reader
        self.bundle_fetcher = bundle_fetcher
        self.interval_seconds = interval_seconds
        self.clock = clock
        self._last_signature: tuple[int, str] | None = None
        self._scheduled_hosts: set[str] = set()

    def enqueue_due(self) -> dict[str, ContentBundle]:
        config = self.consent_reader()
        if not config.enabled:
            return {}
        target_ids = tuple(Path(target).name for target in config.targets)
        if not target_ids:
            return {}
        material = json.dumps(
            {
                "backend_policy": config.backend_policy,
                "external_consent": config.external_consent,
                "targets": target_ids,
                "allowed_roots": tuple(str(root) for root in config.allowed_roots),
                "max_file_bytes": config.max_file_bytes,
                "max_bundle_bytes": config.max_bundle_bytes,
                "excerpt_max_chars": config.excerpt_max_chars,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        material_hash = hashlib.sha256(material.encode()).hexdigest()
        bucket = int(self.clock() // self.interval_seconds)
        signature = (bucket, material_hash)
        if signature != self._last_signature:
            self._scheduled_hosts.clear()

        discovered: dict[str, ContentBundle] = {}
        for host in self.registry.list_hosts():
            if host.host_id in self._scheduled_hosts:
                continue
            from drover.server.advisory.service import content_consent_operation

            with content_consent_operation():
                live = self.consent_reader()
                if not live.enabled or _content_config_version(
                    live
                ) != _content_config_version(config):
                    return {}
                bundle = self.bundle_fetcher(host.host_id, target_ids)
                enqueue_advisory_check(
                    self.duckdb_path,
                    analyzer_id=MODEL_ANALYZER_ID,
                    target_id=host.host_id,
                    source_version=(
                        f"{bundle.bundle_hash}:scheduled:{bucket}:{material_hash}"
                    ),
                )
                discovered[host.host_id] = bundle
                self._scheduled_hosts.add(host.host_id)
        self._last_signature = signature
        return discovered


class ContentAnalysisWorker:
    """Run one ephemeral model analysis behind two live consent fences."""

    def __init__(
        self,
        *,
        consent_reader: Callable[[], AdvisoryContentConfig],
        bundle_fetcher: ContentBundleFetcher,
        backend_factory: Callable[[AdvisoryContentConfig], AnalysisBackend],
        finding_sink: Callable[[FindingCandidate], str] | None = None,
        duckdb_path: str | Path | None = None,
        repository: AdvisoryRepository | None = None,
        worker_id: str = "content-advisory-worker",
        retry_delay: timedelta = timedelta(seconds=30),
        lease_duration: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        scheduler: ContentAnalysisScheduler | None = None,
    ) -> None:
        self.consent_reader = consent_reader
        self.bundle_fetcher = bundle_fetcher
        self.backend_factory = backend_factory
        self.finding_sink = finding_sink
        self.duckdb_path = Path(duckdb_path) if duckdb_path is not None else None
        self.repository = repository
        self.worker_id = worker_id
        self.retry_delay = retry_delay
        if lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        self.lease_duration = lease_duration
        self.clock = clock
        self.scheduler = scheduler
        self._thread: threading.Thread | None = None

    def run_once(self) -> AdvisoryRunResult:
        """Claim and isolate one pending model-configuration job."""

        from drover.server.advisory.service import content_consent_operation

        prefetched: dict[str, ContentBundle] = {}
        if self.scheduler is not None:
            prefetched = self.scheduler.enqueue_due()
        with content_consent_operation():
            config = self.consent_reader()
            if not config.enabled:
                prefetched.clear()
                return AdvisoryRunResult(skipped=1)
            if self.duckdb_path is None or self.repository is None:
                raise ValueError(
                    "duckdb_path and repository are required for job dispatch"
                )
            job = self._claim_model_job()
            if job is None:
                return AdvisoryRunResult(skipped=1)
            target_ids = tuple(Path(target).name for target in config.targets)
            try:
                host_id = job.subject_key.partition(":")[2]
                result = self.run_model_job(
                    host_id=host_id,
                    target_ids=target_ids,
                    job=job,
                    prefetched_bundle=prefetched.get(host_id),
                )
                if result.status == "succeeded":
                    return AdvisoryRunResult(succeeded=1)
                self._requeue_job(job)
                return AdvisoryRunResult(skipped=1)
            except Exception:  # noqa: BLE001 - isolate model/backend failures
                self._record_content_failure(job)
                log.warning("content advisory job failed")
                return AdvisoryRunResult(failed=1)
            finally:
                prefetched.clear()

    def start(
        self,
        *,
        shutdown_event: threading.Event,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive")
        if self._thread is not None and self._thread.is_alive():
            return

        def _run() -> None:
            while not shutdown_event.is_set():
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001 - keep daemon alive degraded
                    log.warning("content advisory worker loop failed")
                shutdown_event.wait(poll_interval_seconds)

        self._thread = threading.Thread(
            target=_run, name="drover-content-advisory", daemon=True
        )
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _claim_model_job(self) -> Job | None:
        assert self.duckdb_path is not None
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            ledger = Ledger(con)
            now = self.clock()
            AdvisoryWorker._reclaim_stale_leases(ledger, now)
            retry_rows = con.execute(
                """
                SELECT job_id, subject_key FROM pipeline_jobs
                WHERE job_kind = ? AND status = 'retry_wait'
                  AND (next_run_at IS NULL OR next_run_at <= ?)
                ORDER BY priority DESC, created_at, job_id
                """,
                [ADVISORY_JOB_KIND, now],
            ).fetchall()
            for job_id, subject_key in retry_rows:
                if str(subject_key).partition(":")[0] == MODEL_ANALYZER_ID:
                    ledger.requeue_job(str(job_id))
            row = con.execute(
                """
                SELECT job_id, job_kind, subject_key, status, attempt_count,
                       max_attempts, latest_attempt_id, latest_artifact_id
                FROM pipeline_jobs
                WHERE job_kind = ? AND status = 'pending'
                  AND starts_with(subject_key, ?)
                ORDER BY priority DESC, created_at, job_id LIMIT 1
                """,
                [ADVISORY_JOB_KIND, f"{MODEL_ANALYZER_ID}:"],
            ).fetchone()
            if row is None:
                return None
            job = Job(*row)
            ledger.lease_job(
                job.job_id,
                worker_id=self.worker_id,
                lease_expires_at=now + self.lease_duration,
            )
            return ledger.latest_job(ADVISORY_JOB_KIND, job.subject_key)
        finally:
            con.close()

    def _requeue_job(self, job: Job) -> None:
        assert self.duckdb_path is not None
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            Ledger(con).reclaim_lease(
                job.job_id,
                error_category="consent_revoked",
                error_message="content analysis consent changed during attempt",
            )
        finally:
            con.close()

    def _record_content_failure(self, job: Job) -> None:
        assert self.duckdb_path is not None
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            ledger = Ledger(con)
            current = ledger.latest_job(ADVISORY_JOB_KIND, job.subject_key)
            if current is None or current.status != "leased":
                return
            if current.attempt_count >= current.max_attempts:
                ledger.fail_job(
                    current.job_id,
                    error_category="model_analysis_error",
                    error_message="content advisory analysis failed",
                )
                ledger.dead_letter_job(current.job_id)
            else:
                ledger.retry_job(
                    current.job_id,
                    error_category="model_analysis_error",
                    error_message="content advisory analysis failed",
                    next_run_at=self.clock() + self.retry_delay,
                )
        finally:
            con.close()

    def run_model_job(
        self,
        *,
        host_id: str,
        target_ids: Iterable[str],
        job: Job | None = None,
        prefetched_bundle: ContentBundle | None = None,
    ) -> ContentAnalysisResult:
        from drover.server.advisory.service import content_consent_operation

        with content_consent_operation():
            return self._run_model_job(
                host_id=host_id,
                target_ids=target_ids,
                job=job,
                prefetched_bundle=prefetched_bundle,
            )

    def _run_model_job(
        self,
        *,
        host_id: str,
        target_ids: Iterable[str],
        job: Job | None = None,
        prefetched_bundle: ContentBundle | None = None,
    ) -> ContentAnalysisResult:
        requested = tuple(target_ids)
        if not requested:
            raise ValueError("at least one content target ID is required")
        config = self.consent_reader()
        if not config.enabled:
            return ContentAnalysisResult(status="disabled", artifact={})

        bundle: ContentBundle | None = None
        fetched: ContentBundle | None = None
        backend: AnalysisBackend | None = None
        analyzer: ModelConfigurationAnalyzer | None = None
        candidates: list[FindingCandidate] | None = None
        try:
            # This read is adjacent to the fetch. Revocation therefore prevents
            # even an ephemeral content response from being requested.
            config = self.consent_reader()
            if not config.enabled:
                return ContentAnalysisResult(status="revoked", artifact={})
            if prefetched_bundle is None:
                try:
                    fetched = self.bundle_fetcher(host_id, requested)
                except Exception:
                    raise ModelFindingError("content bundle fetch failed") from None
            else:
                fetched = prefetched_bundle
            bundle = validate_content_bundle(
                fetched, host_id=host_id, requested_ids=requested
            )
            fetched = None

            # Backend creation is also fenced so cloud credentials/transports
            # are never selected from stale consent.
            config = self.consent_reader()
            if not config.enabled:
                return ContentAnalysisResult(status="revoked", artifact={})
            try:
                backend = self.backend_factory(config)
            except Exception:
                raise ModelFindingError("analysis backend creation failed") from None
            fenced_backend = _ConsentFencedBackend(
                backend=backend,
                consent_reader=self.consent_reader,
                selected_policy=config.backend_policy,
            )
            analyzer = ModelConfigurationAnalyzer(
                fenced_backend, excerpt_max_chars=config.excerpt_max_chars
            )
            try:
                candidates = analyzer.analyze(bundle)
            except AnalysisConsentRevoked:
                return ContentAnalysisResult(status="revoked", artifact={})

            artifact_base = {
                "bundle_hash": bundle.bundle_hash,
                "target_hashes": [target.content_hash for target in bundle.targets],
            }
            if job is not None:
                artifact = self._record_success(job, candidates, artifact_base)
            else:
                sink = self.finding_sink or _candidate_reference
                artifact = {
                    **artifact_base,
                    "finding_ids": [sink(candidate) for candidate in candidates],
                }
            return ContentAnalysisResult(status="succeeded", artifact=artifact)
        finally:
            # Sever all direct references to redacted content before returning.
            candidates = None
            analyzer = None
            backend = None
            fetched = None
            bundle = None

    def _record_success(
        self,
        job: Job,
        candidates: list[FindingCandidate],
        artifact_base: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if self.duckdb_path is None or self.repository is None:
            raise ValueError(
                "duckdb_path and repository are required to record a content job"
            )
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            con.execute("BEGIN TRANSACTION")
            run_id = job.latest_attempt_id or job.job_id
            finding_ids = [
                self.repository.observe_in_transaction(con, candidate, run_id=run_id)
                for candidate in candidates
            ]
            artifact = {**artifact_base, "finding_ids": finding_ids}
            serialized = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
            ledger = Ledger(con)
            ledger.succeed_job(
                job.job_id,
                artifact=ArtifactSpec(
                    artifact_kind=ADVISORY_ARTIFACT_KIND,
                    subject_key=job.subject_key,
                    storage_uri=f"duckdb://advisory_findings/{job.subject_key}",
                    content_hash=hashlib.sha256(serialized.encode()).hexdigest(),
                    version_token=str(artifact["bundle_hash"]),
                    metadata=artifact,
                ),
                metrics=artifact,
            )
            receipt = con.execute(
                """
                SELECT r.receipt_id, r.status
                FROM pipeline_jobs j
                JOIN pipeline_receipts r ON r.receipt_id = j.caused_by_receipt_id
                WHERE j.job_id = ?
                """,
                [job.job_id],
            ).fetchone()
            if receipt is not None and receipt[1] == "observed":
                ledger.mark_receipt(str(receipt[0]), "applied")
            con.execute("COMMIT")
            return artifact
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()


@dataclass
class _ConsentFencedBackend:
    backend: AnalysisBackend
    consent_reader: Callable[[], AdvisoryContentConfig]
    selected_policy: str

    def complete(self, system: str, user: str) -> str:
        from drover.server.advisory.service import content_consent_operation

        with content_consent_operation():
            config = self.consent_reader()
            if (
                not config.enabled
                or config.backend_policy != self.selected_policy
                or (config.backend_policy == "cloud" and not config.external_consent)
            ):
                raise AnalysisConsentRevoked("content analysis consent was revoked")
            return self.backend.complete(system, user)


def _candidate_reference(candidate: FindingCandidate) -> str:
    return f"{candidate.analyzer_id}:{candidate.rule_id}:{candidate.target_id}"


def _content_config_version(config: AdvisoryContentConfig) -> tuple[object, ...]:
    return (
        config.enabled,
        config.backend_policy,
        config.external_consent,
        config.targets,
        config.allowed_roots,
        config.max_file_bytes,
        config.max_bundle_bytes,
        config.excerpt_max_chars,
    )


class AdvisoryWorker:
    """Lease and run at most one durable job for each supplied analyzer."""

    def __init__(
        self,
        *,
        duckdb_path: str | Path,
        repository: AdvisoryRepository,
        snapshot_factory: SnapshotFactory,
        worker_id: str = "advisory-worker",
        retry_delay: timedelta = timedelta(seconds=30),
        lease_duration: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self.repository = repository
        self.snapshot_factory = snapshot_factory
        self.worker_id = worker_id
        self.retry_delay = retry_delay
        if lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        self.lease_duration = lease_duration
        self.clock = clock
        self._thread: threading.Thread | None = None

    def run_once(self, analyzers: Iterable[Analyzer]) -> AdvisoryRunResult:
        succeeded = failed = skipped = 0
        for analyzer in analyzers:
            job = self._claim_for_analyzer(analyzer.analyzer_id)
            if job is None:
                skipped += 1
                continue
            try:
                self._execute(analyzer, job)
                succeeded += 1
            except Exception as exc:  # noqa: BLE001 - isolate analyzer failures
                failed += 1
                self._record_failure(job, exc)
                log.warning(
                    "advisory analyzer %s failed for %s: %s",
                    analyzer.analyzer_id,
                    job.subject_key,
                    exc,
                )
        return AdvisoryRunResult(succeeded=succeeded, failed=failed, skipped=skipped)

    def start(
        self,
        *,
        analyzers: Iterable[Analyzer],
        scheduler: AdvisoryScheduler,
        shutdown_event: threading.Event,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        """Start one bounded daemon loop that exits through ``shutdown_event``."""

        if poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive")
        if self._thread is not None and self._thread.is_alive():
            return
        analyzer_set = tuple(analyzers)

        def _run() -> None:
            while not shutdown_event.is_set():
                try:
                    scheduler.enqueue_due_full_review()
                    self.run_once(analyzer_set)
                except Exception:  # noqa: BLE001 - keep server alive when degraded
                    log.exception("advisory worker loop failed; continuing")
                shutdown_event.wait(poll_interval_seconds)

        self._thread = threading.Thread(
            target=_run, name="drover-advisory", daemon=True
        )
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _claim_for_analyzer(self, analyzer_id: str) -> Job | None:
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            ledger = Ledger(con)
            now = self.clock()
            self._reclaim_stale_leases(ledger, now)
            retry_rows = con.execute(
                """
                SELECT job_id, subject_key FROM pipeline_jobs
                WHERE job_kind = ? AND status = 'retry_wait'
                  AND (next_run_at IS NULL OR next_run_at <= ?)
                ORDER BY priority DESC, created_at, job_id
                """,
                [ADVISORY_JOB_KIND, now],
            ).fetchall()
            for job_id, subject_key in retry_rows:
                if str(subject_key).partition(":")[0] == analyzer_id:
                    ledger.requeue_job(str(job_id))

            rows = con.execute(
                """
                SELECT job_id, job_kind, subject_key, status, attempt_count,
                       max_attempts, latest_attempt_id, latest_artifact_id
                FROM pipeline_jobs
                WHERE job_kind = ? AND status = 'pending'
                ORDER BY priority DESC, created_at, job_id
                """,
                [ADVISORY_JOB_KIND],
            ).fetchall()
            job = next(
                (
                    Job(*row)
                    for row in rows
                    if str(row[2]).partition(":")[0] == analyzer_id
                ),
                None,
            )
            if job is None:
                return None
            ledger.lease_job(
                job.job_id,
                worker_id=self.worker_id,
                lease_expires_at=now + self.lease_duration,
            )
            return ledger.latest_job(ADVISORY_JOB_KIND, job.subject_key)
        finally:
            con.close()

    @staticmethod
    def _reclaim_stale_leases(ledger: Ledger, now: datetime) -> None:
        for job in ledger.list_leased_jobs(
            job_kind=ADVISORY_JOB_KIND, stale_before=now
        ):
            if job.attempt_count >= job.max_attempts:
                ledger.fail_job(
                    job.job_id,
                    error_category="lease_expired",
                    error_message="advisory lease expired at the attempt limit",
                )
                ledger.dead_letter_job(job.job_id)
            else:
                ledger.reclaim_lease(
                    job.job_id,
                    error_category="lease_expired",
                    error_message="advisory lease expired before completion",
                )

    def _execute(self, analyzer: Analyzer, job: Job) -> None:
        target_id = job.subject_key.partition(":")[2]
        source_version = self._source_version(job.job_id)
        snapshot = self.snapshot_factory(
            analyzer.analyzer_id, target_id, source_version
        )
        if snapshot.source_version != source_version:
            raise ValueError("snapshot source version does not match the durable job")

        existing = {
            item.finding_id: item
            for item in self.repository.list_findings()
            if item.analyzer_id == analyzer.analyzer_id
            and _finding_in_job_scope(target_id, item.target_id)
            and item.state not in {FindingState.RESOLVED, FindingState.DISMISSED}
        }
        affected: list[str] = []
        observed_fingerprints: set[str] = set()
        for candidate in analyzer.analyze(snapshot):
            if candidate.analyzer_id != analyzer.analyzer_id:
                raise ValueError(
                    "analyzer emitted a candidate with another analyzer_id"
                )
            finding = self.repository.observe(
                candidate, run_id=job.latest_attempt_id or job.job_id
            )
            affected.append(finding.finding_id)
            observed_fingerprints.add(finding.fingerprint)
        for finding in existing.values():
            if (
                finding.fingerprint not in observed_fingerprints
                and _snapshot_covers_finding(
                    snapshot, finding.target_type, finding.target_id
                )
            ):
                passing = self.repository.mark_passing(
                    finding.finding_id, run_id=job.latest_attempt_id or job.job_id
                )
                affected.append(passing.finding_id)

        finding_ids = tuple(dict.fromkeys(affected))
        payload = json.dumps(finding_ids, separators=(",", ":"))
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            con.execute("BEGIN TRANSACTION")
            ledger = Ledger(con)
            ledger.succeed_job(
                job.job_id,
                artifact=ArtifactSpec(
                    artifact_kind=ADVISORY_ARTIFACT_KIND,
                    subject_key=job.subject_key,
                    storage_uri=f"duckdb://advisory_findings/{job.subject_key}",
                    content_hash=hashlib.sha256(payload.encode()).hexdigest(),
                    version_token=source_version,
                    metadata={"finding_ids": finding_ids},
                ),
                metrics={"finding_count": len(finding_ids)},
            )
            receipt = con.execute(
                """
                SELECT r.receipt_id, r.status
                FROM pipeline_jobs j
                JOIN pipeline_receipts r ON r.receipt_id = j.caused_by_receipt_id
                WHERE j.job_id = ?
                """,
                [job.job_id],
            ).fetchone()
            if receipt is not None and receipt[1] == "observed":
                ledger.mark_receipt(str(receipt[0]), "applied")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()
        self._enqueue_newer_source(job, source_version)

    def _record_failure(self, job: Job, exc: Exception) -> None:
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            ledger = Ledger(con)
            current = ledger.latest_job(ADVISORY_JOB_KIND, job.subject_key)
            if current is None or current.status != "leased":
                return
            if current.attempt_count >= current.max_attempts:
                ledger.fail_job(
                    current.job_id,
                    error_category="analyzer_error",
                    error_message=str(exc),
                )
                ledger.dead_letter_job(current.job_id)
            else:
                ledger.retry_job(
                    current.job_id,
                    error_category="analyzer_error",
                    error_message=str(exc),
                    next_run_at=self.clock() + self.retry_delay,
                )
        finally:
            con.close()

    def _source_version(self, job_id: str) -> str:
        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            row = con.execute(
                """
                SELECT r.source_version
                FROM pipeline_jobs j
                JOIN pipeline_receipts r ON r.receipt_id = j.caused_by_receipt_id
                WHERE j.job_id = ?
                """,
                [job_id],
            ).fetchone()
        finally:
            con.close()
        if row is None or not row[0]:
            raise RuntimeError("advisory job has no source-version receipt")
        return str(row[0])

    def _enqueue_newer_source(self, job: Job, processed_version: str) -> None:
        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            row = con.execute(
                """
                SELECT source_version FROM pipeline_receipts
                WHERE source_kind = 'advisory_target_snapshot' AND source_key = ?
                ORDER BY first_seen_at DESC, receipt_id DESC LIMIT 1
                """,
                [job.subject_key],
            ).fetchone()
        finally:
            con.close()
        if row is not None and row[0] and str(row[0]) != processed_version:
            analyzer_id, _, target_id = job.subject_key.partition(":")
            enqueue_advisory_check(
                self.duckdb_path,
                analyzer_id=analyzer_id,
                target_id=target_id,
                source_version=str(row[0]),
                force=True,
            )


def _snapshot_covers_finding(
    snapshot: AnalysisSnapshot, target_type: str, target_id: str
) -> bool:
    """Require concrete passing evidence before resolving a prior finding."""

    if target_type == "provider_connector":
        return any(
            f"{item.host_id}/{item.provider}/{item.account_label}" == target_id
            for item in snapshot.provider_connections
        )
    if target_type == "telemetry_source":
        return any(item.target_id == target_id for item in snapshot.telemetry)
    if target_type == "routing_policy":
        return any(item.target_id == target_id for item in snapshot.routing)
    if target_type == "hook":
        return any(
            f"{item.host_id}/{item.harness_id}/{item.hook_id}" == target_id
            for item in snapshot.hooks
        )
    return False


def _finding_in_job_scope(job_target_id: str, finding_target_id: str) -> bool:
    return (
        job_target_id == "fleet"
        or finding_target_id == job_target_id
        or finding_target_id.startswith(f"{job_target_id}/")
    )


def load_operational_snapshot(
    duckdb_path: str | Path,
    _analyzer_id: str,
    target_id: str,
    source_version: str,
) -> AnalysisSnapshot:
    """Build a bounded, credential-free snapshot from durable connector state.

    Other fact families remain empty until their bounded query producers have
    evidence. The worker therefore cannot treat them as passing evidence.
    """

    analyzed_at = datetime.now(timezone.utc)
    con = open_duckdb_connection(Path(duckdb_path), read_only=True, role="diagnostic")
    try:
        params: list[object] = []
        where = ""
        if target_id != "fleet":
            where = "WHERE host_id = ?"
            params.append(target_id)
        rows = con.execute(
            f"""
            SELECT provider, account_label, host_id, enabled, last_attempt_at,
                   last_success_at, error_category, updated_at
            FROM provider_connections
            {where}
            ORDER BY host_id, provider, account_label
            LIMIT {MAX_SNAPSHOT_RECORDS}
            """,
            params,
        ).fetchall()
        windows = con.execute(
            f"""
            WITH chosen AS (
              SELECT provider, account_label, host_id,
                     arg_max(snapshot_id, observed_at) FILTER (
                       WHERE status IN ('ok', 'usage_unavailable')
                     ) AS snapshot_id
              FROM provider_usage_snapshots
              {where}
              GROUP BY provider, account_label, host_id
            )
            SELECT p.provider, p.account_label, p.host_id, p.window_kind,
                   p.starts_at, p.resets_at
            FROM provider_usage_snapshots p
            JOIN chosen c USING (provider, account_label, host_id, snapshot_id)
            WHERE p.window_kind IS NOT NULL
            ORDER BY p.host_id, p.provider, p.account_label, p.window_kind
            LIMIT {MAX_SNAPSHOT_RECORDS}
            """,
            params,
        ).fetchall()
    finally:
        con.close()
    windows_by_account: dict[tuple[str, str, str], list[ProviderResetWindow]] = {}
    for provider, account_label, host_id, kind, starts_at, resets_at in windows:
        windows_by_account.setdefault(
            (str(provider), str(account_label), str(host_id)), []
        ).append(
            ProviderResetWindow(
                kind=str(kind), starts_at=starts_at, resets_at=resets_at
            )
        )
    providers = tuple(
        ProviderConnectionObservation(
            provider=str(row[0]),
            account_label=str(row[1]),
            host_id=str(row[2]),
            enabled=bool(row[3]),
            status="error" if row[6] else "ok",
            observed_at=row[7] or row[4] or row[5] or analyzed_at,
            last_attempt_at=row[4],
            last_success_at=row[5],
            error_category=str(row[6]) if row[6] else None,
            reset_windows=tuple(
                windows_by_account.get((str(row[0]), str(row[1]), str(row[2])), ())
            ),
            source_ref=f"provider_connections:{row[2]}/{row[0]}/{row[1]}",
        )
        for row in rows
    )
    return AnalysisSnapshot(
        source_version=source_version,
        analyzed_at=analyzed_at,
        provider_connections=providers,
    )


def operational_analyzers() -> tuple[Analyzer, ...]:
    """Return only analyzers backed by complete runtime snapshot producers."""

    return (ConnectorFreshnessAnalyzer(), ProviderResetWindowAnalyzer())


__all__ = [
    "AdvisoryRunResult",
    "AdvisoryWorker",
    "ContentAnalysisResult",
    "ContentAnalysisScheduler",
    "ContentAnalysisWorker",
    "SnapshotFactory",
    "load_operational_snapshot",
    "operational_analyzers",
]
