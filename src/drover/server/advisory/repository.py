"""DuckDB repository for advisory findings and their bounded evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from uuid import uuid4

import duckdb

from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    Finding,
    FindingCandidate,
    FindingEvidence,
    FindingState,
    JSONValue,
    Severity,
)
from drover.server.db import open_duckdb_connection

MAX_EXCERPT_CHARS = 512
MAX_EVIDENCE_RECORDS = 16
MAX_EVIDENCE_JSON_BYTES = 4096
MAX_EVIDENCE_STRING_CHARS = 512
_FORBIDDEN_CONTENT_KEYS = frozenset(
    {
        "config_content",
        "document_content",
        "full_content",
        "prompt_content",
        "raw_content",
    }
)
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|private[_-]?key|secret|token|api[_-]?key)",
    re.IGNORECASE,
)
_AUTHORIZATION_SECRET = re.compile(
    r"(?im)\b(authorization\s*[:=]\s*)(?:(?:basic|bearer|digest)\s+)?[^\r\n,;]+"
)
_BEARER_SECRET = re.compile(r"(?i)\b(bearer)\s+[^\s,;]+")
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b(([a-z0-9_-]*(?:api[_-]?key|authorization|cookie|credential|password|private[_-]?key|secret|token))\s*[:=]\s*)[^\s,;]+"
)
_PEM_PRIVATE_KEY = re.compile(
    r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?(?:-----END [^-\r\n]*PRIVATE KEY-----|$)"
)
_SEVERITY_RANK = {
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class AdvisoryRepository:
    """Persist findings without applying any configuration changes."""

    def __init__(self, duckdb_path: str | Path) -> None:
        self.duckdb_path = Path(duckdb_path)

    def observe(self, candidate: FindingCandidate, *, run_id: str) -> Finding:
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            con.execute("BEGIN TRANSACTION")
            finding_id = self.observe_in_transaction(con, candidate, run_id=run_id)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()
        return self.get_finding(finding_id)

    def observe_in_transaction(
        self,
        con: duckdb.DuckDBPyConnection,
        candidate: FindingCandidate,
        *,
        run_id: str,
    ) -> str:
        """Observe one candidate using the caller's existing transaction."""

        if not run_id.strip():
            raise ValueError("run_id is required")
        normalized = tuple(_normalize_evidence(item) for item in candidate.evidence)
        if len(normalized) > MAX_EVIDENCE_RECORDS:
            raise ValueError(f"evidence exceeds {MAX_EVIDENCE_RECORDS} records")
        title = _redact_text(candidate.title)
        impact = _redact_text(candidate.impact)
        remediation = tuple(_redact_text(step) for step in candidate.remediation)
        fingerprint = _fingerprint(candidate)
        material_hash = _material_hash(
            impact=impact,
            remediation=remediation,
            normalized=normalized,
        )
        observed_at = max(item[0].observed_at for item in normalized)
        row = con.execute(
            "SELECT finding_id, state, severity, evaluated_content_hash "
            "FROM advisory_findings WHERE fingerprint = ?",
            [fingerprint],
        ).fetchone()
        if row is None:
            finding_id = uuid4().hex
            state = FindingState.OPEN
            con.execute(
                """
                INSERT INTO advisory_findings (
                  finding_id, fingerprint, analyzer_id, rule_id, target_type,
                  target_id, analyzer_class, severity, confidence, title,
                  impact, remediation_json, state, first_seen_at, last_seen_at,
                  evaluated_content_hash, latest_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    finding_id,
                    fingerprint,
                    candidate.analyzer_id,
                    candidate.rule_id,
                    candidate.target_type,
                    candidate.target_id,
                    candidate.analyzer_class.value,
                    candidate.severity.value,
                    candidate.confidence.value,
                    title,
                    impact,
                    json.dumps(remediation),
                    state.value,
                    observed_at,
                    observed_at,
                    candidate.content_hash,
                    run_id,
                ],
            )
        else:
            finding_id = str(row[0])
            old_state = FindingState(row[1])
            state = self._next_observed_state(
                con,
                finding_id=finding_id,
                old_state=old_state,
                old_severity=Severity(row[2]),
                new_severity=candidate.severity,
                old_content_hash=row[3],
                new_content_hash=candidate.content_hash,
                material_hash=material_hash,
            )
            regressed_at = observed_at if old_state == FindingState.RESOLVED else None
            clear_dismissal = (
                old_state == FindingState.DISMISSED and state == FindingState.OPEN
            )
            con.execute(
                """
                UPDATE advisory_findings SET
                  analyzer_class = ?, severity = ?, confidence = ?, title = ?,
                  impact = ?, remediation_json = ?, state = ?,
                  dismissal_reason = CASE WHEN ? THEN NULL ELSE dismissal_reason END,
                  dismissed_at = CASE WHEN ? THEN NULL ELSE dismissed_at END,
                  resolved_at = CASE WHEN ? THEN NULL ELSE resolved_at END,
                  regressed_at = COALESCE(?, regressed_at),
                  last_seen_at = ?, evaluated_content_hash = ?, latest_run_id = ?
                WHERE finding_id = ?
                """,
                [
                    candidate.analyzer_class.value,
                    candidate.severity.value,
                    candidate.confidence.value,
                    title,
                    impact,
                    json.dumps(remediation),
                    state.value,
                    clear_dismissal,
                    clear_dismissal,
                    old_state == FindingState.RESOLVED,
                    regressed_at,
                    observed_at,
                    candidate.content_hash,
                    run_id,
                    finding_id,
                ],
            )
        self._insert_occurrences(
            con,
            finding_id=finding_id,
            run_id=run_id,
            normalized=normalized,
            material_hash=material_hash,
        )
        return finding_id

    def mark_passing(self, finding_id: str, *, run_id: str) -> Finding:
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            con.execute("BEGIN TRANSACTION")
            self.mark_passing_in_transaction(con, finding_id, run_id=run_id)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()
        return self.get_finding(finding_id)

    def mark_passing_in_transaction(
        self,
        con: duckdb.DuckDBPyConnection,
        finding_id: str,
        *,
        run_id: str,
    ) -> None:
        """Resolve one finding using the caller's existing transaction."""

        if not run_id.strip():
            raise ValueError("run_id is required")
        now = datetime.now(timezone.utc)
        self._require_finding(con, finding_id)
        con.execute(
            """
            UPDATE advisory_findings
            SET state = ?, resolved_at = ?, latest_run_id = ?
            WHERE finding_id = ?
            """,
            [FindingState.RESOLVED.value, now, run_id, finding_id],
        )
        con.execute(
            """
            INSERT INTO advisory_occurrences (
              occurrence_id, finding_id, run_id, outcome, observed_at
            ) VALUES (?, ?, ?, 'passing', ?)
            """,
            [uuid4().hex, finding_id, run_id, now],
        )

    def acknowledge(self, finding_id: str) -> Finding:
        return self._operator_transition(
            finding_id,
            allowed={FindingState.OPEN, FindingState.REGRESSED},
            state=FindingState.ACKNOWLEDGED,
        )

    def dismiss(self, finding_id: str, *, reason: str) -> Finding:
        reason = reason.strip()
        if not reason:
            raise ValueError("dismissal reason is required")
        return self._operator_transition(
            finding_id,
            allowed={
                FindingState.OPEN,
                FindingState.ACKNOWLEDGED,
                FindingState.REGRESSED,
            },
            state=FindingState.DISMISSED,
            reason=reason,
        )

    def list_findings(self) -> list[Finding]:
        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            rows = con.execute(
                f"SELECT {_FINDING_COLUMNS} FROM advisory_findings "
                "ORDER BY last_seen_at DESC, finding_id"
            ).fetchall()
        finally:
            con.close()
        return [_finding_from_row(row) for row in rows]

    def get_finding(self, finding_id: str) -> Finding:
        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            row = con.execute(
                f"SELECT {_FINDING_COLUMNS} FROM advisory_findings WHERE finding_id = ?",
                [finding_id],
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise KeyError(f"finding {finding_id!r} not found")
        return _finding_from_row(row)

    def _next_observed_state(
        self,
        con: duckdb.DuckDBPyConnection,
        *,
        finding_id: str,
        old_state: FindingState,
        old_severity: Severity,
        new_severity: Severity,
        old_content_hash: str | None,
        new_content_hash: str | None,
        material_hash: str,
    ) -> FindingState:
        if old_state == FindingState.RESOLVED:
            return FindingState.REGRESSED
        if old_state != FindingState.DISMISSED:
            return old_state
        latest = con.execute(
            "SELECT evidence_hash FROM advisory_occurrences "
            "WHERE finding_id = ? AND outcome = 'failing' "
            "ORDER BY recorded_at DESC, occurrence_id DESC LIMIT 1",
            [finding_id],
        ).fetchone()
        material_change = latest is None or latest[0] != material_hash
        changed_content = old_content_hash != new_content_hash
        increased_severity = _SEVERITY_RANK[new_severity] > _SEVERITY_RANK[old_severity]
        if material_change or changed_content or increased_severity:
            return FindingState.OPEN
        return FindingState.DISMISSED

    def _insert_occurrences(
        self,
        con: duckdb.DuckDBPyConnection,
        *,
        finding_id: str,
        run_id: str,
        normalized: Sequence[tuple[FindingEvidence, str, str | None]],
        material_hash: str,
    ) -> None:
        for evidence, fields_json, excerpt in normalized:
            con.execute(
                """
                INSERT INTO advisory_occurrences (
                  occurrence_id, finding_id, run_id, outcome, observed_at,
                  source_ref, evidence_json, excerpt, evidence_hash
                ) VALUES (?, ?, ?, 'failing', ?, ?, ?, ?, ?)
                """,
                [
                    uuid4().hex,
                    finding_id,
                    run_id,
                    evidence.observed_at,
                    evidence.source_ref,
                    fields_json,
                    excerpt,
                    material_hash,
                ],
            )

    def _operator_transition(
        self,
        finding_id: str,
        *,
        allowed: set[FindingState],
        state: FindingState,
        reason: str | None = None,
    ) -> Finding:
        con = open_duckdb_connection(self.duckdb_path, role="worker")
        try:
            con.execute("BEGIN TRANSACTION")
            current = FindingState(self._require_finding(con, finding_id)[0])
            if current not in allowed:
                raise ValueError(
                    f"cannot {state.value} finding in {current.value} state"
                )
            now = datetime.now(timezone.utc)
            row = con.execute(
                f"""
                UPDATE advisory_findings
                SET state = ?, dismissal_reason = ?,
                    dismissed_at = CASE WHEN ? = 'dismissed' THEN ? ELSE dismissed_at END
                WHERE finding_id = ? AND state = ?
                RETURNING {_FINDING_COLUMNS}
                """,
                [state.value, reason, state.value, now, finding_id, current.value],
            ).fetchone()
            if row is None:
                raise ValueError("finding state changed during lifecycle transition")
            con.execute("COMMIT")
            return _finding_from_row(row)
        except duckdb.TransactionException as exc:
            try:
                con.execute("ROLLBACK")
            except duckdb.Error:
                pass
            raise ValueError(
                "finding state changed during lifecycle transition"
            ) from exc
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    @staticmethod
    def _require_finding(
        con: duckdb.DuckDBPyConnection, finding_id: str
    ) -> tuple[Any, ...]:
        row = con.execute(
            "SELECT state FROM advisory_findings WHERE finding_id = ?", [finding_id]
        ).fetchone()
        if row is None:
            raise KeyError(f"finding {finding_id!r} not found")
        return row


_FINDING_COLUMNS = """
finding_id, fingerprint, analyzer_id, rule_id, target_type, target_id,
analyzer_class, severity, confidence, title, impact, remediation_json, state,
dismissal_reason, first_seen_at, last_seen_at, resolved_at, dismissed_at,
regressed_at, evaluated_content_hash, latest_run_id
"""


def _finding_from_row(row: Sequence[Any]) -> Finding:
    return Finding(
        finding_id=row[0],
        fingerprint=row[1],
        analyzer_id=row[2],
        rule_id=row[3],
        target_type=row[4],
        target_id=row[5],
        analyzer_class=AnalyzerClass(row[6]),
        severity=Severity(row[7]),
        confidence=Confidence(row[8]),
        title=row[9],
        impact=row[10],
        remediation=tuple(json.loads(row[11])),
        state=FindingState(row[12]),
        dismissal_reason=row[13],
        first_seen_at=row[14],
        last_seen_at=row[15],
        resolved_at=row[16],
        dismissed_at=row[17],
        regressed_at=row[18],
        evaluated_content_hash=row[19],
        latest_run_id=row[20],
    )


def _fingerprint(candidate: FindingCandidate) -> str:
    stable = [
        candidate.analyzer_id,
        candidate.rule_id,
        candidate.target_type,
        candidate.target_id,
    ]
    return hashlib.sha256(
        json.dumps(stable, separators=(",", ":")).encode()
    ).hexdigest()


def _material_hash(
    *,
    impact: str,
    remediation: tuple[str, ...],
    normalized: Sequence[tuple[FindingEvidence, str, str | None]],
) -> str:
    material = {
        "impact": impact,
        "remediation": remediation,
        "evidence": [
            {
                "source_ref": evidence.source_ref,
                "fields": json.loads(fields),
                "excerpt": excerpt,
            }
            for evidence, fields, excerpt in normalized
        ],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _normalize_evidence(
    evidence: FindingEvidence,
) -> tuple[FindingEvidence, str, str | None]:
    forbidden = _find_forbidden_key(evidence.fields)
    if forbidden:
        raise ValueError(
            f"full configuration content is not allowed in evidence field {forbidden!r}"
        )
    redacted_fields = _redact_value(evidence.fields)
    fields_json = json.dumps(redacted_fields, sort_keys=True, separators=(",", ":"))
    if len(fields_json.encode()) > MAX_EVIDENCE_JSON_BYTES:
        raise ValueError(f"evidence JSON exceeds {MAX_EVIDENCE_JSON_BYTES} bytes")
    excerpt = evidence.excerpt
    if excerpt is not None:
        if len(excerpt) > MAX_EXCERPT_CHARS:
            raise ValueError(f"excerpt exceeds {MAX_EXCERPT_CHARS} characters")
        excerpt = _redact_text(excerpt)
    return evidence, fields_json, excerpt


def _find_forbidden_key(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN_CONTENT_KEYS:
                return str(key)
            nested = _find_forbidden_key(item)
            if nested:
                return nested
    elif isinstance(value, (list, tuple)):
        for item in value:
            nested = _find_forbidden_key(item)
            if nested:
                return nested
    return None


def _redact_value(value: JSONValue, *, key: str | None = None) -> JSONValue:
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        clipped = value[:MAX_EVIDENCE_STRING_CHARS]
        return _redact_text(clipped)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError(f"evidence contains unsupported value {type(value).__name__}")


def _redact_text(value: str) -> str:
    value = _PEM_PRIVATE_KEY.sub("[REDACTED]", value)
    value = _AUTHORIZATION_SECRET.sub(
        lambda match: f"{match.group(1)}[REDACTED]", value
    )
    value = _BEARER_SECRET.sub(lambda match: f"{match.group(1)} [REDACTED]", value)
    return _ASSIGNED_SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
