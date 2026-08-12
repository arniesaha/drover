"""Isolated advisory analyzer execution over the durable pipeline ledger."""

from __future__ import annotations

from dataclasses import asdict, dataclass
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
    HookDescriptor,
    ProviderConnectionObservation,
    ProviderResetWindow,
    RoutingAggregate,
    TelemetryAggregate,
)
from drover.server.advisory.analyzers.connectors import (
    ConnectorFreshnessAnalyzer,
    ProviderResetWindowAnalyzer,
)
from drover.server.advisory.analyzers.hooks import HookValidityAnalyzer
from drover.server.advisory.analyzers.routing import RoutingMismatchAnalyzer
from drover.server.advisory.analyzers.telemetry import (
    CacheReadEfficiencyAnalyzer,
    TelemetryCoverageAnalyzer,
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
from drover.server.advisory.types import FindingCandidate
from drover.server.db import attached_control_plane_snapshot, open_duckdb_connection
from drover.server.ledger import ArtifactSpec, Job, Ledger

log = logging.getLogger("drover.advisory")
SnapshotFactory = Callable[[str, str, str], AnalysisSnapshot]
MAX_SNAPSHOT_SPANS = 4096
MAX_SNAPSHOT_SPANS_PER_SESSION = 64
# Immutable span evidence is the deterministic latest slice from this bounded
# lookback. The cap+1 probe marks a busy slice incomplete without feeding more
# than MAX_RAW_SNAPSHOT_SPANS rows into any join, window, or aggregate.
MAX_RAW_SNAPSHOT_SPANS = 8192
MAX_RESET_WINDOWS_PER_CONNECTION = 32
OPERATIONAL_SPAN_LOOKBACK = timedelta(days=7)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _job_source_version(duckdb_path: Path, job_id: str) -> str | None:
    con = open_duckdb_connection(duckdb_path, read_only=True, role="diagnostic")
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
    return str(row[0]) if row is not None and row[0] is not None else None


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


class ContentVersionFetcher(Protocol):
    def __call__(self, host_id: str, target_ids: tuple[str, ...]) -> str: ...


class ContentAnalysisScheduler:
    """Discover content hashes and enqueue model jobs without fetching content."""

    def __init__(
        self,
        *,
        duckdb_path: str | Path,
        registry,
        consent_reader: Callable[[], AdvisoryContentConfig],
        version_fetcher: ContentVersionFetcher,
        interval_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("content review interval must be positive")
        self.duckdb_path = Path(duckdb_path)
        self.registry = registry
        self.consent_reader = consent_reader
        self.version_fetcher = version_fetcher
        self.interval_seconds = interval_seconds
        self.clock = clock
        self._last_host_signatures: dict[str, tuple[int, str, str]] = {}
        self.last_failures: dict[str, str] = {}

    def enqueue_due(self) -> dict[str, str]:
        from drover.server.advisory.service import content_consent_generation

        config = self.consent_reader()
        if not config.enabled:
            return {}
        consent_generation = content_consent_generation()
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
                "consent_generation": consent_generation,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        material_hash = hashlib.sha256(material.encode()).hexdigest()
        bucket = int(self.clock() // self.interval_seconds)
        discovered: dict[str, str] = {}
        failures: dict[str, str] = {}
        for host in self.registry.list_hosts():
            from drover.server.advisory.service import content_consent_operation

            try:
                with content_consent_operation() as live_generation:
                    live = self.consent_reader()
                    if (
                        live_generation != consent_generation
                        or not live.enabled
                        or _content_config_version(live)
                        != _content_config_version(config)
                    ):
                        self.last_failures = failures
                        return {}
                    bundle_hash = self.version_fetcher(host.host_id, target_ids)
                    if not _is_sha256(bundle_hash):
                        raise ValueError("content version must be a SHA-256 hash")
                    signature = (bucket, material_hash, bundle_hash)
                    if self._last_host_signatures.get(host.host_id) == signature:
                        continue
                    source_version = f"{bundle_hash}:scheduled:{bucket}:{material_hash}"
                    job = enqueue_advisory_check(
                        self.duckdb_path,
                        analyzer_id=MODEL_ANALYZER_ID,
                        target_id=host.host_id,
                        source_version=source_version,
                    )
                    if (
                        _job_source_version(self.duckdb_path, job.job_id)
                        != source_version
                    ):
                        # A currently leased older generation cannot be retargeted.
                        # Leave the signature unsaved so the next poll retries it.
                        continue
                    discovered[host.host_id] = source_version
                    self._last_host_signatures[host.host_id] = signature
            except Exception:  # noqa: BLE001 - isolate one unreachable host
                failures[host.host_id] = "version_probe_failed"
                log.warning(
                    "content advisory version probe failed host=%s", host.host_id
                )
        self.last_failures = failures
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

        if self.scheduler is not None:
            self.scheduler.enqueue_due()
        with content_consent_operation():
            config = self.consent_reader()
            if not config.enabled:
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
                )
                if result.status == "succeeded":
                    return AdvisoryRunResult(succeeded=1)
                self._requeue_job(job)
                return AdvisoryRunResult(skipped=1)
            except Exception:  # noqa: BLE001 - isolate model/backend failures
                self._record_content_failure(job)
                log.warning("content advisory job failed")
                return AdvisoryRunResult(failed=1)

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
            rows = con.execute(
                """
                SELECT job_id, job_kind, subject_key, status, attempt_count,
                       max_attempts, latest_attempt_id, latest_artifact_id
                FROM pipeline_jobs
                WHERE job_kind = ? AND status = 'pending'
                  AND starts_with(subject_key, ?)
                ORDER BY priority DESC, updated_at, created_at, job_id
                """,
                [ADVISORY_JOB_KIND, f"{MODEL_ANALYZER_ID}:"],
            ).fetchall()
            if not rows:
                return None
            job = Job(*rows[0])
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
    ) -> ContentAnalysisResult:
        from drover.server.advisory.service import content_consent_operation

        with content_consent_operation() as consent_generation:
            return self._run_model_job(
                host_id=host_id,
                target_ids=target_ids,
                job=job,
                consent_generation=consent_generation,
            )

    def _run_model_job(
        self,
        *,
        host_id: str,
        target_ids: Iterable[str],
        consent_generation: int,
        job: Job | None = None,
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
            try:
                fetched = self.bundle_fetcher(host_id, requested)
            except Exception:
                raise ModelFindingError("content bundle fetch failed") from None
            bundle = validate_content_bundle(
                fetched, host_id=host_id, requested_ids=requested
            )
            fetched = None
            if job is not None:
                assert self.duckdb_path is not None
                source_version = _job_source_version(self.duckdb_path, job.job_id)
                expected_hash = (
                    source_version.partition(":")[0]
                    if source_version is not None
                    else None
                )
                if _is_sha256(expected_hash) and bundle.bundle_hash != expected_hash:
                    raise ModelFindingError("content changed after version probe")

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
            from drover.server.advisory.service import (
                validate_content_consent_generation,
            )

            # Keep validation and durable publication in one short critical
            # section. Revocation can overlap remote model I/O, but a result
            # from the prior consent epoch can never cross this boundary.
            with validate_content_consent_generation(consent_generation) as current:
                live = self.consent_reader()
                if not current or not live.enabled:
                    return ContentAnalysisResult(status="revoked", artifact={})
                if job is not None:
                    artifact = self._record_success(
                        job,
                        candidates,
                        artifact_base,
                        covered_target_ids=tuple(
                            f"{bundle.host_id}/{target.target_id}"
                            for target in bundle.targets
                        ),
                    )
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
        *,
        covered_target_ids: tuple[str, ...],
    ) -> Mapping[str, Any]:
        if self.duckdb_path is None or self.repository is None:
            raise ValueError(
                "duckdb_path and repository are required to record a content job"
            )
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            con.execute("BEGIN TRANSACTION")
            run_id = job.latest_attempt_id or job.job_id
            observed_fingerprints: set[str] = set()
            finding_ids: list[str] = []
            for candidate in candidates:
                finding_id = self.repository.observe_in_transaction(
                    con, candidate, run_id=run_id
                )
                fingerprint = con.execute(
                    "SELECT fingerprint FROM advisory_findings WHERE finding_id = ?",
                    [finding_id],
                ).fetchone()[0]
                finding_ids.append(finding_id)
                observed_fingerprints.add(str(fingerprint))
            if covered_target_ids:
                placeholders = ", ".join("?" for _ in covered_target_ids)
                existing = con.execute(
                    f"""
                    SELECT finding_id, fingerprint
                    FROM advisory_findings
                    WHERE analyzer_id = ?
                      AND target_type = 'configuration_target'
                      AND state IN ('open', 'acknowledged', 'regressed')
                      AND target_id IN ({placeholders})
                    """,
                    [MODEL_ANALYZER_ID, *covered_target_ids],
                ).fetchall()
                for finding_id, fingerprint in existing:
                    if str(fingerprint) in observed_fingerprints:
                        continue
                    self.repository.mark_passing_in_transaction(
                        con, str(finding_id), run_id=run_id
                    )
                    finding_ids.append(str(finding_id))
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

        candidates = analyzer.analyze(snapshot)
        for candidate in candidates:
            if candidate.analyzer_id != analyzer.analyzer_id:
                raise ValueError(
                    "analyzer emitted a candidate with another analyzer_id"
                )
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            con.execute("BEGIN TRANSACTION")
            run_id = job.latest_attempt_id or job.job_id
            existing = con.execute(
                """
                SELECT finding_id, fingerprint, target_type, target_id
                FROM advisory_findings
                WHERE analyzer_id = ? AND state NOT IN ('resolved', 'dismissed')
                """,
                [analyzer.analyzer_id],
            ).fetchall()
            affected: list[str] = []
            observed_fingerprints: set[str] = set()
            for candidate in candidates:
                finding_id = self.repository.observe_in_transaction(
                    con, candidate, run_id=run_id
                )
                fingerprint = con.execute(
                    "SELECT fingerprint FROM advisory_findings WHERE finding_id = ?",
                    [finding_id],
                ).fetchone()[0]
                affected.append(finding_id)
                observed_fingerprints.add(str(fingerprint))
            for (
                finding_id,
                fingerprint,
                finding_target_type,
                finding_target_id,
            ) in existing:
                if (
                    _finding_in_job_scope(target_id, str(finding_target_id))
                    and str(fingerprint) not in observed_fingerprints
                    and _snapshot_covers_finding(
                        snapshot,
                        analyzer.analyzer_id,
                        str(finding_target_type),
                        str(finding_target_id),
                    )
                ):
                    self.repository.mark_passing_in_transaction(
                        con, str(finding_id), run_id=run_id
                    )
                    affected.append(str(finding_id))

            finding_ids = tuple(dict.fromkeys(affected))
            payload = json.dumps(finding_ids, separators=(",", ":"))
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
    snapshot: AnalysisSnapshot,
    analyzer_id: str,
    target_type: str,
    target_id: str,
) -> bool:
    """Require concrete passing evidence before resolving a prior finding."""

    if target_type == "provider_connector":
        return any(
            f"{item.host_id}/{item.provider}/{item.account_label}" == target_id
            and (
                analyzer_id != ProviderResetWindowAnalyzer.analyzer_id
                or item.reset_windows_complete
            )
            for item in snapshot.provider_connections
        )
    if target_type == "telemetry_source":
        return any(
            item.target_id == target_id and item.facts_complete
            for item in snapshot.telemetry
        )
    if target_type == "routing_policy":
        return any(
            item.target_id == target_id and item.facts_complete
            for item in snapshot.routing
        )
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
    analyzer_id: str,
    target_id: str,
    source_version: str,
    *,
    analyzed_at: datetime | None = None,
) -> AnalysisSnapshot:
    """Build analyzer-scoped bounded facts from normalized runtime state."""

    analyzed_at = analyzed_at or datetime.now(timezone.utc)
    con = open_duckdb_connection(Path(duckdb_path), read_only=True, role="diagnostic")
    try:
        providers: tuple[ProviderConnectionObservation, ...] = ()
        telemetry: tuple[TelemetryAggregate, ...] = ()
        routing: tuple[RoutingAggregate, ...] = ()
        hooks: tuple[HookDescriptor, ...] = ()
        # Three of these four fact loaders read control-plane tables --
        # `harness_sessions` joined to `spans_enriched`, and `harness_hosts`
        # for the hook descriptors -- which since #95 live in their own
        # database. This attaches a private copy of that (small) store so the
        # queries below can stay exactly as they were, without this analytical
        # reader ever touching the live control-plane file.
        with attached_control_plane_snapshot(con, Path(duckdb_path)):
            if analyzer_id in {
                ConnectorFreshnessAnalyzer.analyzer_id,
                ProviderResetWindowAnalyzer.analyzer_id,
            }:
                providers = _load_provider_facts(con, target_id, analyzed_at)
            elif analyzer_id in {
                TelemetryCoverageAnalyzer.analyzer_id,
                CacheReadEfficiencyAnalyzer.analyzer_id,
            }:
                telemetry = _load_telemetry_facts(con, target_id, analyzed_at)
            elif analyzer_id == RoutingMismatchAnalyzer.analyzer_id:
                routing = _load_routing_facts(con, target_id, analyzed_at)
            elif analyzer_id == HookValidityAnalyzer.analyzer_id:
                hooks = _load_hook_facts(con, target_id, analyzed_at)
            else:
                raise ValueError(f"unsupported operational analyzer: {analyzer_id}")
    finally:
        con.close()
    return AnalysisSnapshot(
        source_version=source_version,
        analyzed_at=analyzed_at,
        provider_connections=providers,
        telemetry=telemetry,
        routing=routing,
        hooks=hooks,
    )


def operational_analyzers() -> tuple[Analyzer, ...]:
    """Return analyzers whose complete bounded facts are produced above."""

    return (
        ConnectorFreshnessAnalyzer(),
        ProviderResetWindowAnalyzer(),
        TelemetryCoverageAnalyzer(),
        RoutingMismatchAnalyzer(),
        CacheReadEfficiencyAnalyzer(),
        HookValidityAnalyzer(),
    )


def _host_filter(target_id: str, *, alias: str = "") -> tuple[str, list[object]]:
    if target_id == "fleet":
        return "", []
    prefix = f"{alias}." if alias else ""
    return f"WHERE {prefix}host_id = ?", [target_id]


def _runtime_session_filter(
    target_id: str, analyzed_at: datetime, *, alias: str = "h"
) -> tuple[str, list[object]]:
    prefix = f"{alias}." if alias else ""
    latest_activity = (
        f"GREATEST({prefix}updated_at, {prefix}ended_at, {prefix}started_at)"
    )
    clauses = [
        f"{latest_activity} >= ?",
        f"{latest_activity} <= ?",
    ]
    params: list[object] = [
        analyzed_at - OPERATIONAL_SPAN_LOOKBACK,
        analyzed_at,
    ]
    if target_id != "fleet":
        parts = target_id.split("/")
        clauses.extend([f"{prefix}host_id = ?", f"{prefix}harness = ?"])
        params.extend(parts[:2])
    return "WHERE " + " AND ".join(clauses), params


def _aware(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _optional_aware(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value, datetime.now(timezone.utc))


def _load_provider_facts(con, target_id: str, analyzed_at: datetime):
    where, params = _host_filter(target_id)
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
        WITH bounded_connections AS (
          SELECT provider, account_label, host_id
          FROM provider_connections
          {where}
          ORDER BY host_id, provider, account_label
          LIMIT {MAX_SNAPSHOT_RECORDS}
        ),
        chosen AS (
          SELECT p.provider, p.account_label, p.host_id,
                 arg_max(snapshot_id, observed_at) FILTER (
                   WHERE status IN ('ok', 'usage_unavailable')
                 ) AS snapshot_id
          FROM provider_usage_snapshots p
          JOIN bounded_connections c USING (provider, account_label, host_id)
          GROUP BY p.provider, p.account_label, p.host_id
        ),
        ranked AS (
          SELECT p.provider, p.account_label, p.host_id, p.window_kind,
                 p.starts_at, p.resets_at,
                 count(p.window_kind) OVER (
                   PARTITION BY p.provider, p.account_label, p.host_id
                 ) AS window_count,
                 row_number() OVER (
                   PARTITION BY p.provider, p.account_label, p.host_id
                   ORDER BY p.window_kind NULLS LAST, p.starts_at, p.resets_at
                 ) AS window_no
          FROM provider_usage_snapshots p
          JOIN chosen c USING (provider, account_label, host_id, snapshot_id)
        )
        SELECT provider, account_label, host_id, window_kind, starts_at,
               resets_at, window_count
        FROM ranked
        WHERE window_no <= {MAX_RESET_WINDOWS_PER_CONNECTION}
        ORDER BY host_id, provider, account_label, window_no
        """,
        params,
    ).fetchall()
    windows_by_account: dict[tuple[str, str, str], list[ProviderResetWindow]] = {}
    reset_completeness: dict[tuple[str, str, str], bool] = {}
    for provider, account_label, host_id, kind, starts_at, resets_at, count in windows:
        key = (str(provider), str(account_label), str(host_id))
        reset_completeness[key] = 0 < int(count) <= MAX_RESET_WINDOWS_PER_CONNECTION
        if kind is not None:
            windows_by_account.setdefault(key, []).append(
                ProviderResetWindow(
                    kind=str(kind),
                    starts_at=_optional_aware(starts_at),
                    resets_at=_optional_aware(resets_at),
                )
            )
    return tuple(
        ProviderConnectionObservation(
            provider=str(row[0]),
            account_label=str(row[1]),
            host_id=str(row[2]),
            enabled=bool(row[3]),
            status="error" if row[6] else "ok",
            observed_at=_aware(row[7] or row[4] or row[5], analyzed_at),
            last_attempt_at=_optional_aware(row[4]),
            last_success_at=_optional_aware(row[5]),
            error_category=str(row[6]) if row[6] else None,
            reset_windows=tuple(
                windows_by_account.get((str(row[0]), str(row[1]), str(row[2])), ())
            ),
            reset_windows_complete=reset_completeness.get(
                (str(row[0]), str(row[1]), str(row[2])), False
            ),
            source_ref=f"provider_connections:{row[2]}/{row[0]}/{row[1]}",
        )
        for row in rows
    )


def _load_telemetry_facts(con, target_id: str, analyzed_at: datetime):
    where, params = _runtime_session_filter(target_id, analyzed_at)
    rows = con.execute(
        f"""
        WITH ranked_sessions AS (
          SELECT h.*,
                 row_number() OVER (
                   ORDER BY GREATEST(updated_at, ended_at, started_at) DESC,
                            session_id
                 ) AS snapshot_session_no,
                 count(*) OVER () AS snapshot_session_count
          FROM harness_sessions h
          {where}
        ),
        bounded_sessions AS (
          SELECT * FROM ranked_sessions
          WHERE snapshot_session_no <= {MAX_SNAPSHOT_RECORDS}
        ),
        raw_span_candidates AS (
          SELECT s.span_id, s.session_id, s.start_time, s.total_tokens,
                 s.prompt_tokens, s.cost_usd, s.cache_read_tokens
          FROM spans_enriched s
          JOIN bounded_sessions h USING (session_id)
          WHERE s.start_time >= ? AND s.start_time <= ?
          ORDER BY s.start_time DESC NULLS LAST, s.span_id
          LIMIT {MAX_RAW_SNAPSHOT_SPANS + 1}
        ),
        raw_span_status AS (
          SELECT count(*) > {MAX_RAW_SNAPSHOT_SPANS} AS raw_spans_truncated,
                 least(count(*), {MAX_RAW_SNAPSHOT_SPANS})::BIGINT
                   AS input_span_records
          FROM raw_span_candidates
        ),
        raw_spans AS (
          SELECT * FROM raw_span_candidates
          ORDER BY start_time DESC NULLS LAST, span_id
          LIMIT {MAX_RAW_SNAPSHOT_SPANS}
        ),
        ranked_spans AS (
          SELECT s.*,
                 row_number() OVER (
                   PARTITION BY s.session_id
                   ORDER BY s.start_time DESC NULLS LAST, s.span_id
                 ) AS session_span_no,
                 count(*) OVER (PARTITION BY s.session_id) AS session_span_count
          FROM raw_spans s
          JOIN bounded_sessions h USING (session_id)
        ),
        per_session_spans AS (
          SELECT * FROM ranked_spans
          WHERE session_span_no <= {MAX_SNAPSHOT_SPANS_PER_SESSION}
        ),
        span_candidates AS (
          SELECT *,
                 row_number() OVER (
                   ORDER BY start_time DESC NULLS LAST, span_id
                 ) AS global_span_no,
                 count(*) OVER () AS bounded_span_count
          FROM per_session_spans
        ),
        span_status AS (
          SELECT count(*) > {MAX_SNAPSHOT_SPANS} AS global_spans_truncated
          FROM per_session_spans
        ),
        bounded_spans AS (
          SELECT * FROM span_candidates
          WHERE global_span_no <= {MAX_SNAPSHOT_SPANS}
        ),
        span_sessions AS (
          SELECT s.session_id, max(s.start_time) AS observed_at,
                 count(*) > 0 AS has_spans,
                 bool_or(s.total_tokens IS NOT NULL OR s.prompt_tokens IS NOT NULL)
                   AS has_tokens,
                 bool_or(s.cost_usd IS NOT NULL) AS has_cost,
                 COALESCE(sum(s.prompt_tokens), 0)::BIGINT AS prompt_tokens,
                 COALESCE(sum(s.cache_read_tokens), 0)::BIGINT AS cache_read_tokens,
                 bool_or(
                   s.session_span_count > {MAX_SNAPSHOT_SPANS_PER_SESSION}
                   OR s.bounded_span_count > {MAX_SNAPSHOT_SPANS}
                 ) AS spans_truncated
          FROM bounded_spans s
          GROUP BY s.session_id
        )
        SELECT h.host_id, h.harness,
               max(GREATEST(s.observed_at, h.updated_at, h.ended_at, h.started_at)),
               count(DISTINCT h.session_id),
               count(DISTINCT h.session_id) FILTER (WHERE s.has_spans),
               count(DISTINCT h.session_id) FILTER (
                 WHERE h.repo_owner IS NOT NULL AND h.repo_name IS NOT NULL
               ),
               count(DISTINCT h.session_id) FILTER (WHERE s.has_tokens),
               count(DISTINCT h.session_id) FILTER (WHERE s.has_cost),
               COALESCE(sum(s.prompt_tokens), 0)::BIGINT,
               COALESCE(sum(s.cache_read_tokens), 0)::BIGINT,
               NOT (
                 max(h.snapshot_session_count) > {MAX_SNAPSHOT_RECORDS}
                 OR any_value(raw_bounds.raw_spans_truncated)
                 OR any_value(bounds.global_spans_truncated)
                 OR COALESCE(bool_or(s.spans_truncated), FALSE)
               ),
               any_value(raw_bounds.input_span_records)
        FROM bounded_sessions h
        LEFT JOIN span_sessions s USING (session_id)
        CROSS JOIN span_status bounds
        CROSS JOIN raw_span_status raw_bounds
        GROUP BY h.host_id, h.harness
        ORDER BY h.host_id, h.harness
        LIMIT {MAX_SNAPSHOT_RECORDS}
        """,
        [
            *params,
            analyzed_at - OPERATIONAL_SPAN_LOOKBACK,
            analyzed_at,
        ],
    ).fetchall()
    return tuple(
        TelemetryAggregate(
            target_id=f"{row[0]}/{row[1]}",
            host_id=str(row[0]),
            harness_id=str(row[1]),
            observed_at=_aware(row[2], analyzed_at),
            total_sessions=int(row[3]),
            sessions_with_spans=int(row[4]),
            repository_attributed_sessions=int(row[5]),
            token_observed_sessions=int(row[6]),
            cost_observed_sessions=int(row[7]),
            prompt_tokens=int(row[8]),
            cache_read_tokens=int(row[9]),
            facts_complete=bool(row[10]),
            input_span_records=int(row[11]),
            source_ref=f"normalized-telemetry:{row[0]}/{row[1]}",
        )
        for row in rows
    )


def _load_routing_facts(con, target_id: str, analyzed_at: datetime):
    where, params = _runtime_session_filter(target_id, analyzed_at)
    target_parts = target_id.split("/")
    provider = (
        target_parts[2] if target_id != "fleet" and len(target_parts) == 3 else None
    )
    rows = con.execute(
        f"""
        WITH ranked_sessions AS (
          SELECT h.*,
                 row_number() OVER (
                   ORDER BY GREATEST(updated_at, ended_at, started_at) DESC,
                            session_id
                 ) AS snapshot_session_no,
                 count(*) OVER () AS snapshot_session_count
          FROM harness_sessions h
          {where}
        ),
        bounded_sessions AS (
          SELECT * FROM ranked_sessions
          WHERE snapshot_session_no <= {MAX_SNAPSHOT_RECORDS}
        ),
        raw_span_candidates AS (
          SELECT s.span_id, s.session_id, s.start_time, s.routing_provider,
                 s.llm_provider, s.routing_model
          FROM spans_enriched s
          JOIN bounded_sessions h USING (session_id)
          WHERE s.start_time >= ? AND s.start_time <= ?
          ORDER BY s.start_time DESC NULLS LAST, s.span_id
          LIMIT {MAX_RAW_SNAPSHOT_SPANS + 1}
        ),
        raw_span_status AS (
          SELECT count(*) > {MAX_RAW_SNAPSHOT_SPANS} AS raw_spans_truncated,
                 least(count(*), {MAX_RAW_SNAPSHOT_SPANS})::BIGINT
                   AS input_span_records
          FROM raw_span_candidates
        ),
        raw_spans AS (
          SELECT * FROM raw_span_candidates
          ORDER BY start_time DESC NULLS LAST, span_id
          LIMIT {MAX_RAW_SNAPSHOT_SPANS}
        ),
        ranked_spans AS (
          SELECT s.*,
                 row_number() OVER (
                   PARTITION BY s.session_id
                   ORDER BY s.start_time DESC NULLS LAST, s.span_id
                 ) AS session_span_no,
                 count(*) OVER (PARTITION BY s.session_id) AS session_span_count
          FROM raw_spans s
          JOIN bounded_sessions h USING (session_id)
        ),
        per_session_spans AS (
          SELECT * FROM ranked_spans
          WHERE session_span_no <= {MAX_SNAPSHOT_SPANS_PER_SESSION}
        ),
        span_candidates AS (
          SELECT *,
                 row_number() OVER (
                   ORDER BY start_time DESC NULLS LAST, span_id
                 ) AS global_span_no,
                 count(*) OVER () AS bounded_span_count
          FROM per_session_spans
        ),
        bounded_spans AS (
          SELECT * FROM span_candidates
          WHERE global_span_no <= {MAX_SNAPSHOT_SPANS}
        )
        SELECT h.host_id, h.harness,
               COALESCE(s.routing_provider, s.llm_provider, 'unknown') AS provider,
               max(GREATEST(s.start_time, h.updated_at, h.ended_at, h.started_at)),
               count(*),
               count(*) FILTER (WHERE s.routing_model <> h.model),
               NOT (
                 max(h.snapshot_session_count) > {MAX_SNAPSHOT_RECORDS}
                 OR any_value(raw_bounds.raw_spans_truncated)
                 OR bool_or(
                   s.session_span_count > {MAX_SNAPSHOT_SPANS_PER_SESSION}
                   OR s.bounded_span_count > {MAX_SNAPSHOT_SPANS}
                 )
               ),
               any_value(raw_bounds.input_span_records)
        FROM bounded_sessions h
        JOIN bounded_spans s USING (session_id)
        CROSS JOIN raw_span_status raw_bounds
        WHERE h.model IS NOT NULL
          AND s.routing_model IS NOT NULL
          AND (? IS NULL OR COALESCE(s.routing_provider, s.llm_provider, 'unknown') = ?)
        GROUP BY h.host_id, h.harness, provider
        ORDER BY h.host_id, h.harness, provider
        LIMIT {MAX_SNAPSHOT_RECORDS}
        """,
        [
            *params,
            analyzed_at - OPERATIONAL_SPAN_LOOKBACK,
            analyzed_at,
            provider,
            provider,
        ],
    ).fetchall()
    return tuple(
        RoutingAggregate(
            target_id=f"{row[0]}/{row[1]}/{row[2]}",
            host_id=str(row[0]),
            harness_id=str(row[1]),
            provider=str(row[2]),
            observed_at=_aware(row[3], analyzed_at),
            decision_count=int(row[4]),
            mismatch_count=int(row[5]),
            facts_complete=bool(row[6]),
            input_span_records=int(row[7]),
            source_ref=f"normalized-routing:{row[0]}/{row[1]}/{row[2]}",
        )
        for row in rows
    )


def _load_hook_facts(con, target_id: str, analyzed_at: datetime):
    parts = target_id.split("/")
    host_scope = parts[0] if target_id != "fleet" else "fleet"
    harness_scope = parts[1] if len(parts) == 3 else None
    hook_scope = parts[2] if len(parts) == 3 else None
    where, params = _host_filter(host_scope)
    rows = con.execute(
        f"""
        SELECT host_id, capabilities_json, updated_at, last_seen_at
        FROM harness_hosts
        {where}
        ORDER BY host_id
        LIMIT {MAX_SNAPSHOT_RECORDS}
        """,
        params,
    ).fetchall()
    descriptors: list[HookDescriptor] = []
    for host_id, raw_capabilities, updated_at, last_seen_at in rows:
        try:
            capabilities = json.loads(raw_capabilities or "{}")
            hooks = capabilities.get("advisory", {}).get("hooks", [])
        except (AttributeError, TypeError, ValueError):
            continue
        if not isinstance(hooks, list):
            continue
        for raw in hooks:
            if len(descriptors) >= MAX_SNAPSHOT_RECORDS:
                return tuple(descriptors)
            if not isinstance(raw, dict) or raw.get("allowlisted") is not True:
                continue
            if harness_scope is not None and (
                raw.get("harness_id") != harness_scope
                or raw.get("hook_id") != hook_scope
            ):
                continue
            try:
                descriptors.append(
                    HookDescriptor(
                        hook_id=str(raw["hook_id"]),
                        host_id=str(host_id),
                        harness_id=str(raw["harness_id"]),
                        canonical_config_path=str(raw["canonical_config_path"]),
                        canonical_executable_path=str(raw["canonical_executable_path"]),
                        target_hash=str(raw["target_hash"]),
                        enabled=raw["enabled"],
                        executable_exists=raw["executable_exists"],
                        executable_is_file=raw["executable_is_file"],
                        executable_is_executable=raw["executable_is_executable"],
                        allowlisted=True,
                        observed_at=_aware(updated_at or last_seen_at, analyzed_at),
                        source_ref=f"harness-inventory:{host_id}/{raw['harness_id']}/hooks/{raw['hook_id']}",
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    return tuple(descriptors)


def operational_snapshot_source_version(
    duckdb_path: str | Path,
    analyzer_id: str,
    target_id: str,
    *,
    analyzed_at: datetime | None = None,
) -> str:
    """Hash only the complete analyzer facts so unchanged reviews coalesce."""

    analyzed_at = analyzed_at or datetime.now(timezone.utc)
    snapshot = load_operational_snapshot(
        duckdb_path,
        analyzer_id,
        target_id,
        "operational-facts:material",
        analyzed_at=analyzed_at,
    )
    material = json.dumps(
        _operational_material(snapshot, analyzer_id),
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: (
            value.isoformat() if isinstance(value, datetime) else str(value)
        ),
    )
    return f"operational-facts:{hashlib.sha256(material.encode()).hexdigest()}"


def _operational_material(
    snapshot: AnalysisSnapshot, analyzer_id: str
) -> list[dict[str, Any]]:
    if analyzer_id == ConnectorFreshnessAnalyzer.analyzer_id:
        maximum_age_seconds = int(ConnectorFreshnessAnalyzer().max_age.total_seconds())
        return [
            {
                "provider": item.provider,
                "account_label": item.account_label,
                "host_id": item.host_id,
                "enabled": item.enabled,
                "status": item.status,
                "error_category": item.error_category,
                "freshness_stale": item.status == "stale"
                or int(
                    (
                        snapshot.analyzed_at
                        - (item.last_success_at or item.observed_at)
                    ).total_seconds()
                )
                > maximum_age_seconds,
            }
            for item in snapshot.provider_connections
        ]
    if analyzer_id == ProviderResetWindowAnalyzer.analyzer_id:
        return [
            {
                "provider": item.provider,
                "account_label": item.account_label,
                "host_id": item.host_id,
                "enabled": item.enabled,
                "reset_windows_complete": item.reset_windows_complete,
                "reset_windows": [asdict(window) for window in item.reset_windows],
            }
            for item in snapshot.provider_connections
        ]
    if analyzer_id in {
        TelemetryCoverageAnalyzer.analyzer_id,
        CacheReadEfficiencyAnalyzer.analyzer_id,
    }:
        return [
            {
                "target_id": item.target_id,
                "host_id": item.host_id,
                "harness_id": item.harness_id,
                "total_sessions": item.total_sessions,
                "sessions_with_spans": item.sessions_with_spans,
                "repository_attributed_sessions": item.repository_attributed_sessions,
                "token_observed_sessions": item.token_observed_sessions,
                "cost_observed_sessions": item.cost_observed_sessions,
                "prompt_tokens": item.prompt_tokens,
                "cache_read_tokens": item.cache_read_tokens,
                "facts_complete": item.facts_complete,
                "input_span_records": item.input_span_records,
            }
            for item in snapshot.telemetry
        ]
    if analyzer_id == RoutingMismatchAnalyzer.analyzer_id:
        return [
            {
                "target_id": item.target_id,
                "host_id": item.host_id,
                "harness_id": item.harness_id,
                "provider": item.provider,
                "decision_count": item.decision_count,
                "mismatch_count": item.mismatch_count,
                "facts_complete": item.facts_complete,
                "input_span_records": item.input_span_records,
            }
            for item in snapshot.routing
        ]
    if analyzer_id == HookValidityAnalyzer.analyzer_id:
        return [
            {
                "hook_id": item.hook_id,
                "host_id": item.host_id,
                "harness_id": item.harness_id,
                "canonical_config_path": item.canonical_config_path,
                "canonical_executable_path": item.canonical_executable_path,
                "target_hash": item.target_hash,
                "enabled": item.enabled,
                "executable_exists": item.executable_exists,
                "executable_is_file": item.executable_is_file,
                "executable_is_executable": item.executable_is_executable,
            }
            for item in snapshot.hooks
        ]
    raise ValueError(f"unsupported operational analyzer: {analyzer_id}")


__all__ = [
    "AdvisoryRunResult",
    "AdvisoryWorker",
    "ContentAnalysisResult",
    "ContentAnalysisScheduler",
    "ContentAnalysisWorker",
    "SnapshotFactory",
    "load_operational_snapshot",
    "operational_snapshot_source_version",
    "operational_analyzers",
]
