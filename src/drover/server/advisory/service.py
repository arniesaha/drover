"""Read-only advisory presentation and explicit lifecycle actions."""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from drover.config import default_config, default_config_path, load_config
from drover.server.advisory.jobs import (
    ADVISORY_JOB_KIND,
    ADVISORY_RECEIPT_KIND,
    LIGHTWEIGHT_ANALYZER_IDS,
    enqueue_advisory_check,
)
from drover.server.advisory.model_analyzer import MODEL_ANALYZER_ID
from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    Finding,
    FindingState,
    Severity,
)
from drover.server.db import open_duckdb_connection
from drover.server.harness.content_consent import DurableContentConsent

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_DETAIL_EVIDENCE = 16
_FINDING_ID = re.compile(r"^[0-9a-f]{32}$")
_SEVERITY_RANK_SQL = (
    "CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 "
    "WHEN 'medium' THEN 2 WHEN 'low' THEN 1 END"
)


class _ContentConsentCoordinator:
    """Track active content operations without locking across remote I/O."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._mutation_lock = threading.Lock()
        self._condition = threading.Condition(self.lock)
        self._active = 0
        self._revoking = False
        self._generation = 0

    @contextmanager
    def operation(self):
        with self._condition:
            self._active += 1
            generation = self._generation
        try:
            yield generation
        finally:
            with self._condition:
                self._active -= 1
                if self._active == 0:
                    self._condition.notify_all()

    def wait_for_idle(self) -> None:
        # The caller holds ``lock`` while persisting disabled consent. Condition
        # waits release it so active operations can drain, while new operations
        # can only observe the already-disabled configuration.
        while self._active:
            self._condition.wait()

    def begin_revocation(self) -> None:
        self._revoking = True

    def advance_generation(self) -> int:
        self._generation += 1
        return self._generation

    def generation(self) -> int:
        with self._condition:
            return self._generation

    @contextmanager
    def validate(self, generation: int):
        with self._condition:
            yield generation == self._generation and not self._revoking

    def finish_revocation(self) -> None:
        self._revoking = False
        self._condition.notify_all()

    def wait_for_revocation(self) -> None:
        while self._revoking:
            self._condition.wait()

    @contextmanager
    def mutation(self):
        # Consent mutations include bounded host I/O. Serialize them without
        # holding ``lock``, which remains the content-operation fence.
        with self._mutation_lock:
            yield


_CONTENT_CONSENT_COORDINATOR = _ContentConsentCoordinator()
CONTENT_CONSENT_FENCE = _CONTENT_CONSENT_COORDINATOR.lock
_CONTENT_JOB_STATUSES = ("pending", "leased", "retry_wait")


def content_consent_operation():
    return _CONTENT_CONSENT_COORDINATOR.operation()


def content_consent_generation() -> int:
    return _CONTENT_CONSENT_COORDINATOR.generation()


def validate_content_consent_generation(generation: int):
    return _CONTENT_CONSENT_COORDINATOR.validate(generation)


class InvalidInsightRequest(ValueError):
    """The client supplied an invalid identifier, filter, cursor, or body."""


class InvalidInsightTransition(ValueError):
    """A lifecycle action is not valid for the finding's current state."""


@dataclass(frozen=True)
class InsightFilters:
    state: str | None = None
    severity: str | None = None
    confidence: str | None = None
    analyzer_class: str | None = None
    host: str | None = None
    harness: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE_SIZE

    def __post_init__(self) -> None:
        enums = {
            "state": FindingState,
            "severity": Severity,
            "confidence": Confidence,
            "analyzer_class": AnalyzerClass,
        }
        for name, enum_type in enums.items():
            value = getattr(self, name)
            if value is not None:
                try:
                    enum_type(value)
                except ValueError as exc:
                    raise InvalidInsightRequest(f"invalid {name}") from exc
        for name in ("host", "harness", "target_type", "target_id"):
            value = getattr(self, name)
            if value is not None and (not value.strip() or len(value) > 256):
                raise InvalidInsightRequest(f"invalid {name}")
        if not isinstance(self.limit, int) or not 1 <= self.limit <= MAX_PAGE_SIZE:
            raise InvalidInsightRequest(f"limit must be between 1 and {MAX_PAGE_SIZE}")


class InsightsService:
    """Serialize bounded findings and delegate lifecycle persistence."""

    def __init__(
        self,
        duckdb_path: str | Path,
        *,
        config_path: str | Path | None = None,
        consent_propagator: Callable[[bool, int], list[dict[str, str]]] | None = None,
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self.config_path = Path(config_path or default_config_path()).expanduser()
        self.repository = AdvisoryRepository(self.duckdb_path)
        consent_path = self.config_path.with_name(
            f".{self.config_path.name}.content-consent.json"
        )
        self._content_consent = DurableContentConsent(consent_path)
        self._consent_propagator = consent_propagator

    def set_content_consent_propagator(
        self, propagator: Callable[[bool, int], list[dict[str, str]]]
    ) -> None:
        self._consent_propagator = propagator

    def content_consent_state(self) -> dict[str, Any]:
        with _CONTENT_CONSENT_COORDINATOR.mutation():
            config = (
                load_config(self.config_path).advisory_content
                if self.config_path.exists()
                else default_config().advisory_content
            )
            consent, _ = self._reconcile_content_consent(enabled=config.enabled)
            return consent

    def content_analysis_status(self) -> dict[str, Any]:
        """Return central truth plus a serialized current-epoch fleet reconcile."""

        # A status read may perform bounded, idempotent host consent pushes at
        # exactly the already-durable epoch. Serialize that reconciliation with
        # mutations so a slow GET cannot publish an older fleet result after an
        # enable or revoke has advanced the epoch.
        with _CONTENT_CONSENT_COORDINATOR.mutation():
            config = (
                load_config(self.config_path).advisory_content
                if self.config_path.exists()
                else default_config().advisory_content
            )
            consent, repair_error = self._reconcile_content_consent(
                enabled=config.enabled
            )
            result = {
                "enabled": config.enabled,
                "backend": config.backend_policy,
                "external_disclosure_accepted": config.external_consent,
                "pending_model_jobs": self._pending_model_job_count(),
            }
            if repair_error is None:
                self._append_propagation(result, consent)
            else:
                self._append_repair_failure(result, consent)
            return result

    def consent_content_analysis(
        self,
        *,
        backend: str,
        external_disclosure_accepted: bool,
    ) -> dict[str, Any]:
        with _CONTENT_CONSENT_COORDINATOR.mutation():
            return self._consent_content_analysis(
                backend=backend,
                external_disclosure_accepted=external_disclosure_accepted,
            )

    def _consent_content_analysis(
        self,
        *,
        backend: str,
        external_disclosure_accepted: bool,
    ) -> dict[str, Any]:
        if backend not in {"local", "cloud"}:
            raise InvalidInsightRequest("backend must be local or cloud")
        if type(external_disclosure_accepted) is not bool:
            raise InvalidInsightRequest(
                "external_disclosure_accepted must be a boolean"
            )
        if backend == "cloud" and not external_disclosure_accepted:
            raise InvalidInsightRequest(
                "cloud analysis requires explicit external disclosure acceptance"
            )
        # Local consent never carries cloud disclosure forward implicitly.
        disclosure = backend == "cloud" and external_disclosure_accepted
        with CONTENT_CONSENT_FENCE:
            _CONTENT_CONSENT_COORDINATOR.wait_for_revocation()
            self._persist_content_consent(
                enabled=True,
                backend=backend,
                external_disclosure_accepted=disclosure,
            )
            _CONTENT_CONSENT_COORDINATOR.advance_generation()
            consent = self._advance_content_consent(enabled=True)
            pending = self._pending_model_job_count()
        result = {
            "enabled": True,
            "backend": backend,
            "external_disclosure_accepted": disclosure,
            "pending_model_jobs": pending,
        }
        self._append_propagation(result, consent)
        return result

    def revoke_content_analysis(self) -> dict[str, Any]:
        """Disable model analysis before atomically cancelling runnable jobs."""

        with _CONTENT_CONSENT_COORDINATOR.mutation():
            return self._revoke_content_analysis()

    def _revoke_content_analysis(self) -> dict[str, Any]:
        """Perform one serialized revocation without locking across host I/O."""

        with CONTENT_CONSENT_FENCE:
            _CONTENT_CONSENT_COORDINATOR.begin_revocation()
            try:
                self._persist_content_consent(
                    enabled=False,
                    backend="local",
                    external_disclosure_accepted=False,
                )
                _CONTENT_CONSENT_COORDINATOR.advance_generation()
                consent = self._advance_content_consent(enabled=False)
            except Exception:
                _CONTENT_CONSENT_COORDINATOR.finish_revocation()
                raise
        hosts = self._propagate_content_consent(consent)
        with CONTENT_CONSENT_FENCE:
            try:
                _CONTENT_CONSENT_COORDINATOR.wait_for_idle()
                cancelled = self._cancel_pending_model_jobs()
                pending = self._pending_model_job_count()
            finally:
                _CONTENT_CONSENT_COORDINATOR.finish_revocation()
        result = {
            "enabled": False,
            "backend": "local",
            "external_disclosure_accepted": False,
            "pending_model_jobs": pending,
            "cancelled_model_jobs": cancelled,
        }
        self._append_propagation(result, consent, hosts=hosts)
        return result

    def _advance_content_consent(self, *, enabled: bool) -> dict[str, Any]:
        current = self._content_consent.snapshot()
        return self._content_consent.apply(
            enabled=enabled, epoch=int(current["epoch"]) + 1
        )

    def _reconcile_content_consent(
        self, *, enabled: bool
    ) -> tuple[dict[str, Any], str | None]:
        """Make the durable gate match validated central user intent."""

        with CONTENT_CONSENT_FENCE:
            if self._content_consent.reconciled(enabled=enabled):
                return self._content_consent.snapshot(), None
            try:
                consent = self._content_consent.reconcile(enabled=enabled)
            except Exception:
                consent = self._content_consent.snapshot()
                error = "durable consent repair failed"
            else:
                error = None
            # A repaired or fail-closed gate invalidates any content operation
            # that began under the divergent durable epoch.
            _CONTENT_CONSENT_COORDINATOR.advance_generation()
            return consent, error

    def _propagate_content_consent(
        self, consent: Mapping[str, Any]
    ) -> list[dict[str, str]]:
        if self._consent_propagator is None:
            return []
        try:
            return self._consent_propagator(
                bool(consent["enabled"]), int(consent["epoch"])
            )
        except Exception:
            return [{"host_id": "fleet", "state": "failed"}]

    def _append_propagation(
        self,
        result: dict[str, Any],
        consent: Mapping[str, Any],
        *,
        hosts: list[dict[str, str]] | None = None,
    ) -> None:
        if self._consent_propagator is None:
            return
        if hosts is None:
            hosts = self._propagate_content_consent(consent)
        states = {host.get("state") for host in hosts}
        if "failed" in states:
            propagation = "failed"
        elif "disconnected" in states:
            propagation = "partial"
        else:
            propagation = "complete"
        result.update(
            consent_epoch=int(consent["epoch"]),
            propagation=propagation,
            hosts=hosts,
        )

    @staticmethod
    def _append_repair_failure(
        result: dict[str, Any], consent: Mapping[str, Any]
    ) -> None:
        result.update(
            consent_epoch=int(consent["epoch"]),
            propagation="failed",
            hosts=[
                {
                    "host_id": "fleet",
                    "state": "failed",
                    "error": "durable consent repair failed",
                }
            ],
        )

    def pending_model_jobs(self) -> list[dict[str, str]]:
        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            rows = con.execute(
                """
                SELECT job_id, status FROM pipeline_jobs
                WHERE job_kind = ? AND starts_with(subject_key, ?)
                  AND status IN (?, ?, ?)
                ORDER BY created_at, job_id
                """,
                [
                    ADVISORY_JOB_KIND,
                    f"{MODEL_ANALYZER_ID}:",
                    *_CONTENT_JOB_STATUSES,
                ],
            ).fetchall()
        finally:
            con.close()
        return [{"job_id": str(row[0]), "status": str(row[1])} for row in rows]

    def _pending_model_job_count(self) -> int:
        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            return int(
                con.execute(
                    """
                    SELECT count(*) FROM pipeline_jobs
                    WHERE job_kind = ? AND starts_with(subject_key, ?)
                      AND status IN (?, ?, ?)
                    """,
                    [
                        ADVISORY_JOB_KIND,
                        f"{MODEL_ANALYZER_ID}:",
                        *_CONTENT_JOB_STATUSES,
                    ],
                ).fetchone()[0]
            )
        finally:
            con.close()

    def purge_content_excerpts(self) -> int:
        """Null only bounded excerpts, retaining all lifecycle evidence."""

        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            con.execute("BEGIN TRANSACTION")
            count = int(
                con.execute(
                    "SELECT count(*) FROM advisory_occurrences "
                    "WHERE excerpt IS NOT NULL"
                ).fetchone()[0]
            )
            con.execute(
                "UPDATE advisory_occurrences SET excerpt = NULL "
                "WHERE excerpt IS NOT NULL"
            )
            con.execute("COMMIT")
            return count
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def _cancel_pending_model_jobs(self) -> int:
        from drover.server.ledger import Ledger

        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            con.execute("BEGIN TRANSACTION")
            job_ids = [
                str(row[0])
                for row in con.execute(
                    """
                    SELECT job_id FROM pipeline_jobs
                    WHERE job_kind = ? AND starts_with(subject_key, ?)
                      AND status IN (?, ?, ?)
                    ORDER BY created_at, job_id
                    """,
                    [
                        ADVISORY_JOB_KIND,
                        f"{MODEL_ANALYZER_ID}:",
                        *_CONTENT_JOB_STATUSES,
                    ],
                ).fetchall()
            ]
            ledger = Ledger(con)
            for job_id in job_ids:
                ledger.cancel_job(job_id)
            con.execute("COMMIT")
            return len(job_ids)
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def _persist_content_consent(
        self,
        *,
        enabled: bool,
        backend: str,
        external_disclosure_accepted: bool,
    ) -> None:
        for name, value in {
            "enabled": enabled,
            "external_disclosure_accepted": external_disclosure_accepted,
        }.items():
            if type(value) is not bool:
                raise InvalidInsightRequest(f"{name} must be a boolean")
        original = (
            self.config_path.read_text(encoding="utf-8")
            if self.config_path.exists()
            else ""
        )
        rendered = _replace_advisory_content_values(
            original,
            enabled=enabled,
            backend=backend,
            external_consent=external_disclosure_accepted,
        )
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        mode = (
            self.config_path.stat().st_mode & 0o777
            if self.config_path.exists()
            else 0o600
        )
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.config_path.parent,
                prefix=f".{self.config_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, mode)
            # Validate the complete file before it can replace live consent.
            load_config(Path(temp_name))
            os.replace(temp_name, self.config_path)
            temp_name = None
            directory_fd = os.open(
                self.config_path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)

    def list_insights(self, filters: InsightFilters) -> dict[str, Any]:
        clauses: list[str] = []
        values: list[Any] = []
        for column in ("state", "severity", "confidence", "analyzer_class"):
            value = getattr(filters, column)
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        for column in ("target_type", "target_id"):
            value = getattr(filters, column)
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        if filters.host is not None:
            clauses.append("split_part(target_id, '/', 1) = ?")
            values.append(filters.host)
        if filters.harness is not None:
            clauses.append(
                "target_type IN ('hook', 'telemetry_source', 'routing_policy') "
                "AND split_part(target_id, '/', 2) = ?"
            )
            values.append(filters.harness)

        if filters.cursor is not None:
            rank, last_seen, finding_id = _decode_cursor(filters.cursor)
            clauses.append(
                f"({_SEVERITY_RANK_SQL} < ? OR "
                f"({_SEVERITY_RANK_SQL} = ? AND last_seen_at < ?) OR "
                f"({_SEVERITY_RANK_SQL} = ? AND last_seen_at = ? "
                "AND finding_id > ?))"
            )
            values.extend([rank, rank, last_seen, rank, last_seen, finding_id])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            rows = con.execute(
                f"""
                SELECT finding_id, analyzer_id, rule_id, target_type, target_id,
                       analyzer_class, severity, confidence, title, state,
                       first_seen_at, last_seen_at,
                       {_SEVERITY_RANK_SQL} AS severity_rank
                FROM advisory_findings
                {where}
                ORDER BY severity_rank DESC, last_seen_at DESC, finding_id ASC
                LIMIT ?
                """,
                [*values, filters.limit + 1],
            ).fetchall()
        finally:
            con.close()

        has_more = len(rows) > filters.limit
        page = rows[: filters.limit]
        cursor = (
            _encode_cursor(page[-1][12], page[-1][11], page[-1][0])
            if has_more
            else None
        )
        return {
            "findings": [_summary_from_row(row) for row in page],
            "next_cursor": cursor,
        }

    def get_insight(self, finding_id: str) -> dict[str, Any]:
        finding_id = validate_finding_id(finding_id)
        finding = self.repository.get_finding(finding_id)
        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            rows = con.execute(
                """
                SELECT observed_at, source_ref, evidence_json, excerpt
                FROM advisory_occurrences
                WHERE finding_id = ? AND outcome = 'failing'
                ORDER BY observed_at DESC, recorded_at DESC, occurrence_id DESC
                LIMIT ?
                """,
                [finding_id, MAX_DETAIL_EVIDENCE],
            ).fetchall()
        finally:
            con.close()
        return {
            "finding": _serialize_finding(finding),
            "evidence": [
                {
                    "observed_at": _wire_datetime(row[0]),
                    "source_ref": row[1],
                    "fields": json.loads(row[2]) if row[2] else {},
                    "excerpt": row[3],
                }
                for row in rows
            ],
            "actions": {"check_again": self._check_action(finding)},
        }

    def acknowledge(self, finding_id: str) -> dict[str, Any]:
        finding_id = validate_finding_id(finding_id)
        try:
            finding = self.repository.acknowledge(finding_id)
        except ValueError as exc:
            raise InvalidInsightTransition(str(exc)) from exc
        return {"finding": _serialize_finding(finding)}

    def dismiss(self, finding_id: str, *, reason: str) -> dict[str, Any]:
        finding_id = validate_finding_id(finding_id)
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidInsightRequest("dismissal reason is required")
        if len(reason.strip()) > 1000:
            raise InvalidInsightRequest("dismissal reason is too long")
        try:
            finding = self.repository.dismiss(finding_id, reason=reason)
        except ValueError as exc:
            raise InvalidInsightTransition(str(exc)) from exc
        return {"finding": _serialize_finding(finding)}

    def check_again(self, finding_id: str) -> dict[str, Any]:
        finding_id = validate_finding_id(finding_id)
        finding = self.repository.get_finding(finding_id)
        if finding.analyzer_id == MODEL_ANALYZER_ID:
            with content_consent_operation() as generation:
                target_id, source_version = self._check_scope(finding)
                with validate_content_consent_generation(generation) as current:
                    if not current or not self._content_config().enabled:
                        raise InvalidInsightTransition(
                            "Enable content analysis before checking again."
                        )
                    job = self._enqueue_check(finding, target_id, source_version)
        else:
            target_id, source_version = self._check_scope(finding)
            job = self._enqueue_check(finding, target_id, source_version)
        return {"status": "queued", "job_id": job.job_id}

    def _enqueue_check(self, finding: Finding, target_id: str, source_version: str):
        return enqueue_advisory_check(
            self.duckdb_path,
            analyzer_id=finding.analyzer_id,
            target_id=target_id,
            source_version=source_version,
            force=True,
        )

    def _check_action(self, finding: Finding) -> dict[str, Any]:
        try:
            self._check_scope(finding)
        except InvalidInsightTransition as exc:
            return {"available": False, "reason": str(exc)}
        except Exception:
            return {
                "available": False,
                "reason": "Check Again is temporarily unavailable.",
            }
        return {"available": True, "reason": None}

    def _check_scope(self, finding: Finding) -> tuple[str, str]:
        if (
            finding.target_type == "provider_connector"
            and finding.analyzer_id in LIGHTWEIGHT_ANALYZER_IDS
        ):
            from drover.server.providers.service import (
                provider_operational_source_version,
            )

            parts = finding.target_id.split("/")
            if len(parts) != 3 or not all(parts):
                raise InvalidInsightTransition(
                    "provider finding has no executable host scope"
                )
            host_id = parts[0]
            return host_id, provider_operational_source_version(
                self.duckdb_path, host_id
            )
        if (
            finding.target_type == "configuration_target"
            and finding.analyzer_id == MODEL_ANALYZER_ID
        ):
            config = self._content_config()
            if not config.enabled:
                raise InvalidInsightTransition(
                    "Enable content analysis before checking again."
                )
            host_id, separator, target_id = finding.target_id.partition("/")
            configured_targets = {Path(item).name for item in config.targets}
            if (
                not separator
                or not host_id
                or not target_id
                or target_id not in configured_targets
            ):
                raise InvalidInsightTransition(
                    "Model finding has no currently configured analysis scope."
                )
            source_version = self._latest_source_version(MODEL_ANALYZER_ID, host_id)
            if source_version is None:
                raise InvalidInsightTransition(
                    "Run content analysis before checking again."
                )
            return host_id, source_version
        operational_scopes = {
            "deterministic.telemetry_coverage": ("telemetry_source", 2),
            "deterministic.cache_read_efficiency": ("telemetry_source", 2),
            "deterministic.routing_mismatch": ("routing_policy", 3),
            "deterministic.hook_validity": ("hook", 3),
        }
        expected = operational_scopes.get(finding.analyzer_id)
        if expected is not None and finding.target_type == expected[0]:
            parts = finding.target_id.split("/")
            if len(parts) != expected[1] or not all(parts):
                raise InvalidInsightTransition(
                    "finding has no executable operational target scope"
                )
            from drover.server.advisory.worker import (
                load_operational_snapshot,
                operational_snapshot_source_version,
            )

            snapshot = load_operational_snapshot(
                self.duckdb_path,
                finding.analyzer_id,
                finding.target_id,
                "operational-facts:scope-probe",
            )
            facts = (
                snapshot.hooks
                if finding.analyzer_id == "deterministic.hook_validity"
                else (
                    snapshot.routing
                    if finding.analyzer_id == "deterministic.routing_mismatch"
                    else snapshot.telemetry
                )
            )
            if not facts:
                raise InvalidInsightTransition(
                    "Check Again is unavailable because no current facts exist "
                    "for this finding target."
                )
            return finding.target_id, operational_snapshot_source_version(
                self.duckdb_path, finding.analyzer_id, finding.target_id
            )
        raise InvalidInsightTransition(
            "scoped reanalysis is unavailable for this finding analyzer"
        )

    def _content_config(self):
        return (
            load_config(self.config_path).advisory_content
            if self.config_path.exists()
            else default_config().advisory_content
        )

    def _latest_source_version(self, analyzer_id: str, target_id: str) -> str | None:
        subject_key = f"{analyzer_id}:{target_id}"
        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            row = con.execute(
                """
                SELECT source_version
                FROM pipeline_receipts
                WHERE source_kind = ? AND source_key = ?
                ORDER BY first_seen_at DESC, receipt_id DESC
                LIMIT 1
                """,
                [ADVISORY_RECEIPT_KIND, subject_key],
            ).fetchone()
        finally:
            con.close()
        if row is None or not isinstance(row[0], str) or not row[0]:
            return None
        return row[0]


def validate_finding_id(value: str) -> str:
    if not isinstance(value, str) or _FINDING_ID.fullmatch(value) is None:
        raise InvalidInsightRequest("invalid finding id")
    return value


def validate_action_body(body: Mapping[str, Any], *, allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise InvalidInsightRequest(f"unsupported body field: {unknown[0]}")


def _replace_advisory_content_values(
    source: str,
    *,
    enabled: bool,
    backend: str,
    external_consent: bool,
) -> str:
    """Patch only consent fields in the existing TOML section."""

    section_match = re.search(r"(?m)^\[advisory_content\]\s*(?:#.*)?$", source)
    if section_match is None:
        separator = "" if not source or source.endswith("\n") else "\n"
        return (
            source
            + separator
            + "\n[advisory_content]\n"
            + f"enabled = {str(enabled).lower()}\n"
            + f'backend_policy = "{backend}"\n'
            + f"external_consent = {str(external_consent).lower()}\n"
        )
    next_section = re.search(
        r"(?m)^\[[^\]]+\]\s*(?:#.*)?$", source[section_match.end() :]
    )
    end = (
        section_match.end() + next_section.start()
        if next_section is not None
        else len(source)
    )
    before = source[: section_match.end()]
    section = source[section_match.end() : end]
    after = source[end:]
    replacements = {
        "enabled": str(enabled).lower(),
        "backend_policy": f'"{backend}"',
        "external_consent": str(external_consent).lower(),
    }
    for key, value in replacements.items():
        pattern = re.compile(rf"(?m)^(\s*{re.escape(key)}\s*=\s*).*$")
        if pattern.search(section):
            section = pattern.sub(rf"\g<1>{value}", section, count=1)
        else:
            section = section.rstrip("\n") + f"\n{key} = {value}\n"
    return before + section + after


def _summary_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "finding_id": row[0],
        "analyzer_id": row[1],
        "rule_id": row[2],
        "target_type": row[3],
        "target_id": row[4],
        "analyzer_class": row[5],
        "severity": row[6],
        "confidence": row[7],
        "title": row[8],
        "state": row[9],
        "first_seen_at": _wire_datetime(row[10]),
        "last_seen_at": _wire_datetime(row[11]),
    }


def _serialize_finding(finding: Finding) -> dict[str, Any]:
    return {
        "finding_id": finding.finding_id,
        "analyzer_id": finding.analyzer_id,
        "rule_id": finding.rule_id,
        "target_type": finding.target_type,
        "target_id": finding.target_id,
        "analyzer_class": finding.analyzer_class.value,
        "severity": finding.severity.value,
        "confidence": finding.confidence.value,
        "title": finding.title,
        "impact": finding.impact,
        "remediation": list(finding.remediation),
        "state": finding.state.value,
        "dismissal_reason": finding.dismissal_reason,
        "first_seen_at": _wire_datetime(finding.first_seen_at),
        "last_seen_at": _wire_datetime(finding.last_seen_at),
        "resolved_at": _wire_datetime(finding.resolved_at),
        "dismissed_at": _wire_datetime(finding.dismissed_at),
        "regressed_at": _wire_datetime(finding.regressed_at),
    }


def _encode_cursor(rank: int, last_seen: datetime, finding_id: str) -> str:
    raw = json.dumps(
        [rank, _wire_datetime(last_seen), finding_id], separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[int, datetime, str]:
    try:
        if not value or len(value) > 512:
            raise ValueError
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(decoded, list) or len(decoded) != 3:
            raise ValueError
        rank, timestamp, finding_id = decoded
        if not isinstance(rank, int) or rank not in {1, 2, 3, 4}:
            raise ValueError
        observed_at = datetime.fromisoformat(timestamp)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError
        validate_finding_id(finding_id)
        return rank, observed_at, finding_id
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidInsightRequest("invalid cursor") from exc


def _wire_datetime(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


__all__ = [
    "CONTENT_CONSENT_FENCE",
    "content_consent_operation",
    "content_consent_generation",
    "validate_content_consent_generation",
    "InsightFilters",
    "InsightsService",
    "InvalidInsightRequest",
    "InvalidInsightTransition",
    "validate_action_body",
    "validate_finding_id",
]
