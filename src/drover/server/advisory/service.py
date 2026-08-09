"""Read-only advisory presentation and explicit lifecycle actions."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping

from drover.server.advisory.jobs import (
    LIGHTWEIGHT_ANALYZER_IDS,
    enqueue_advisory_check,
)
from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    Finding,
    FindingState,
    Severity,
)
from drover.server.db import open_duckdb_connection

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_DETAIL_EVIDENCE = 16
_FINDING_ID = re.compile(r"^[0-9a-f]{32}$")
_SEVERITY_RANK_SQL = (
    "CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 "
    "WHEN 'medium' THEN 2 WHEN 'low' THEN 1 END"
)


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

    def __init__(self, duckdb_path: str | Path) -> None:
        self.duckdb_path = Path(duckdb_path)
        self.repository = AdvisoryRepository(self.duckdb_path)

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
        target_id, source_version = self._check_scope(finding)
        job = enqueue_advisory_check(
            self.duckdb_path,
            analyzer_id=finding.analyzer_id,
            target_id=target_id,
            source_version=source_version,
            force=True,
        )
        return {"status": "queued", "job_id": job.job_id}

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
        raise InvalidInsightTransition(
            "scoped reanalysis is unavailable for this finding analyzer"
        )


def validate_finding_id(value: str) -> str:
    if not isinstance(value, str) or _FINDING_ID.fullmatch(value) is None:
        raise InvalidInsightRequest("invalid finding id")
    return value


def validate_action_body(body: Mapping[str, Any], *, allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise InvalidInsightRequest(f"unsupported body field: {unknown[0]}")


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
    "InsightFilters",
    "InsightsService",
    "InvalidInsightRequest",
    "InvalidInsightTransition",
    "validate_action_body",
    "validate_finding_id",
]
