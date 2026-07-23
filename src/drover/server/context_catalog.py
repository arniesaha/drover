"""Metadata-as-code bundle validation, diff, and import helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

import duckdb
import yaml

from drover.server.db import open_duckdb_connection

ALLOWED_SOURCE_STAGES = frozenset({"generated", "edited"})
SUPPORTED_SUFFIXES = frozenset({".md", ".markdown", ".yaml", ".yml"})

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{1,127}$")
_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n(.*))?\Z", re.DOTALL)
_SECRET_KEYWORDS = ("secret", "token", "password", "passwd", "api_key", "apikey")
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
)
_SAFE_SECRET_PLACEHOLDERS = frozenset(
    {"", "<redacted>", "[redacted]", "redacted", "***", "changeme", "example"}
)


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str


@dataclass(frozen=True)
class BundleEntry:
    record_id: str
    kind: str
    title: str
    content_md: str
    refs: tuple[str, ...]
    metadata: dict[str, Any]
    source_stage: str
    source_path: str
    content_hash: str
    normalized_json: str


@dataclass(frozen=True)
class BundleLoadResult:
    entries: list[BundleEntry]
    issues: list[ValidationIssue]
    scanned_files: int


@dataclass(frozen=True)
class DiffItem:
    record_id: str
    kind: str
    source_path: str
    action: str
    changed_fields: tuple[str, ...]


@dataclass(frozen=True)
class DiffSummary:
    records: int
    created: list[DiffItem]
    updated: list[DiffItem]
    unchanged: list[DiffItem]


def load_bundle(bundle_path: Path) -> BundleLoadResult:
    """Read and validate a metadata-as-code bundle from disk."""
    root = Path(bundle_path)
    entries: list[BundleEntry] = []
    issues: list[ValidationIssue] = []
    seen_ids: dict[str, str] = {}
    files = list(_iter_bundle_files(root))

    if not files:
        issues.append(
            ValidationIssue(
                path=str(root),
                message="no Markdown or YAML bundle files were found",
            )
        )
        return BundleLoadResult(entries=[], issues=issues, scanned_files=0)

    for file_path in files:
        try:
            entry = _parse_bundle_file(root, file_path)
        except ValueError as exc:
            issues.append(
                ValidationIssue(path=_display_path(root, file_path), message=str(exc))
            )
            continue

        for secret_issue in _detect_secret_issues(entry):
            issues.append(secret_issue)

        previous = seen_ids.get(entry.record_id)
        display_path = _display_path(root, file_path)
        if previous is not None:
            issues.append(
                ValidationIssue(
                    path=display_path,
                    message=f"duplicate id {entry.record_id!r}; already defined in {previous}",
                )
            )
            continue
        seen_ids[entry.record_id] = display_path
        entries.append(entry)

    known_ids = {entry.record_id for entry in entries}
    for entry in entries:
        for ref in entry.refs:
            if ref not in known_ids:
                issues.append(
                    ValidationIssue(
                        path=entry.source_path,
                        message=f"missing referenced id {ref!r} from refs",
                    )
                )

    return BundleLoadResult(entries=entries, issues=issues, scanned_files=len(files))


def diff_bundle(*, entries: list[BundleEntry], duckdb_path: Path) -> DiffSummary:
    """Compare bundle entries against imported curated records."""
    current = _load_existing_records(duckdb_path)
    created: list[DiffItem] = []
    updated: list[DiffItem] = []
    unchanged: list[DiffItem] = []

    for entry in entries:
        existing = current.get(entry.record_id)
        if existing is None:
            created.append(
                DiffItem(
                    record_id=entry.record_id,
                    kind=entry.kind,
                    source_path=entry.source_path,
                    action="create",
                    changed_fields=(),
                )
            )
            continue
        if existing["content_hash"] == entry.content_hash:
            unchanged.append(
                DiffItem(
                    record_id=entry.record_id,
                    kind=entry.kind,
                    source_path=entry.source_path,
                    action="unchanged",
                    changed_fields=(),
                )
            )
            continue
        changed_fields = _changed_fields(
            json.loads(existing["normalized_json"]), json.loads(entry.normalized_json)
        )
        updated.append(
            DiffItem(
                record_id=entry.record_id,
                kind=entry.kind,
                source_path=entry.source_path,
                action="update",
                changed_fields=tuple(changed_fields),
            )
        )

    return DiffSummary(
        records=len(entries), created=created, updated=updated, unchanged=unchanged
    )


def import_bundle(
    *, entries: list[BundleEntry], duckdb_path: Path, apply: bool
) -> dict[str, Any]:
    """Dry-run or apply curated metadata bundle imports."""
    summary = diff_bundle(entries=entries, duckdb_path=duckdb_path)
    if not apply:
        return {
            "mode": "dry-run",
            "summary": summary,
            "applied": 0,
            "provenance_rows": 0,
        }

    now = datetime.now(timezone.utc)
    diff_by_id = {item.record_id: item for item in [*summary.created, *summary.updated]}
    applied = 0
    provenance_rows = 0

    con = open_duckdb_connection(duckdb_path)
    try:
        con.execute("BEGIN")
        for entry in entries:
            change = diff_by_id.get(entry.record_id)
            if change is None:
                continue
            applied += 1
            con.execute(
                """
                INSERT INTO curated_context_records (
                  record_id,
                  kind,
                  title,
                  content_md,
                  refs,
                  metadata_json,
                  source_stage,
                  source_path,
                  content_hash,
                  normalized_json,
                  created_at,
                  updated_at,
                  imported_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (record_id) DO UPDATE SET
                  kind = EXCLUDED.kind,
                  title = EXCLUDED.title,
                  content_md = EXCLUDED.content_md,
                  refs = EXCLUDED.refs,
                  metadata_json = EXCLUDED.metadata_json,
                  source_stage = EXCLUDED.source_stage,
                  source_path = EXCLUDED.source_path,
                  content_hash = EXCLUDED.content_hash,
                  normalized_json = EXCLUDED.normalized_json,
                  updated_at = EXCLUDED.updated_at,
                  imported_at = EXCLUDED.imported_at
                """,
                [
                    entry.record_id,
                    entry.kind,
                    entry.title,
                    entry.content_md,
                    list(entry.refs),
                    json.dumps(entry.metadata, sort_keys=True),
                    entry.source_stage,
                    entry.source_path,
                    entry.content_hash,
                    entry.normalized_json,
                    now,
                    now,
                    now,
                ],
            )
            details = json.dumps(
                {
                    "action": change.action,
                    "changed_fields": list(change.changed_fields),
                    "source_path": entry.source_path,
                },
                sort_keys=True,
            )
            provenance_rows += _insert_provenance_event(
                con=con,
                record_id=entry.record_id,
                event_kind=entry.source_stage,
                source_path=entry.source_path,
                content_hash=entry.content_hash,
                recorded_at=now,
                details_json=details,
            )
            provenance_rows += _insert_provenance_event(
                con=con,
                record_id=entry.record_id,
                event_kind="imported",
                source_path=entry.source_path,
                content_hash=entry.content_hash,
                recorded_at=now,
                details_json=details,
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()

    return {
        "mode": "apply",
        "summary": summary,
        "applied": applied,
        "provenance_rows": provenance_rows,
    }


def format_validation_issues(issues: list[ValidationIssue]) -> str:
    lines = ["context validation failed"]
    for issue in issues:
        lines.append(f"  {issue.path}: {issue.message}")
    return "\n".join(lines)


def format_diff(summary: DiffSummary, *, heading: str) -> str:
    lines = [
        heading,
        f"records={summary.records} created={len(summary.created)} "
        f"updated={len(summary.updated)} unchanged={len(summary.unchanged)}",
    ]
    for item in [*summary.created, *summary.updated, *summary.unchanged]:
        lines.append(_format_diff_item(item))
    return "\n".join(lines)


def _format_diff_item(item: DiffItem) -> str:
    details = ""
    if item.changed_fields:
        details = f" changed={','.join(item.changed_fields)}"
    return (
        f"  {item.action:9s} id={item.record_id} kind={item.kind} "
        f"path={item.source_path}{details}"
    )


def _iter_bundle_files(bundle_path: Path) -> list[Path]:
    if bundle_path.is_file():
        return [bundle_path] if bundle_path.suffix.lower() in SUPPORTED_SUFFIXES else []
    return sorted(
        file_path
        for file_path in bundle_path.rglob("*")
        if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def _display_path(root: Path, file_path: Path) -> str:
    if root.is_file():
        return file_path.name
    return str(file_path.relative_to(root))


def _parse_bundle_file(root: Path, file_path: Path) -> BundleEntry:
    raw = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        frontmatter, body = _parse_markdown(raw, file_path)
    else:
        frontmatter, body = _parse_yaml_document(raw, file_path)
    return _entry_from_document(
        source_path=_display_path(root, file_path),
        frontmatter=frontmatter,
        body=body,
    )


def _parse_markdown(raw: str, file_path: Path) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError("Markdown bundle files must start with YAML frontmatter")
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise ValueError("frontmatter must be a YAML mapping")
    body = (match.group(2) or "").strip()
    return frontmatter, body


def _parse_yaml_document(raw: str, file_path: Path) -> tuple[dict[str, Any], str]:
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML document: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("YAML bundle files must contain a top-level mapping")
    body = document.pop("body", document.pop("content_md", "")) or ""
    if body is not None and not isinstance(body, str):
        raise ValueError("body/content_md must be a string when present")
    return document, body.strip()


def _entry_from_document(
    *, source_path: str, frontmatter: dict[str, Any], body: str
) -> BundleEntry:
    doc = dict(frontmatter)
    record_id = _require_string(doc.pop("id", None), "id")
    if not _ID_RE.match(record_id):
        raise ValueError(
            f"id {record_id!r} is invalid; use 2-128 chars from [A-Za-z0-9._:/-]"
        )

    kind = _require_string(doc.pop("kind", None), "kind").lower()
    if not _KIND_RE.match(kind):
        raise ValueError(
            f"kind {kind!r} is invalid; use lowercase slug characters [a-z0-9._-]"
        )

    title = _require_string(doc.pop("title", None), "title")

    refs_raw = doc.pop("refs", [])
    if refs_raw is None:
        refs_raw = []
    if not isinstance(refs_raw, list):
        raise ValueError("refs must be a list of record IDs")
    refs: list[str] = []
    for ref in refs_raw:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("refs must contain only non-empty strings")
        cleaned = ref.strip()
        if not _ID_RE.match(cleaned):
            raise ValueError(f"ref {cleaned!r} is invalid")
        refs.append(cleaned)
    if len(set(refs)) != len(refs):
        raise ValueError("refs must not contain duplicates")

    provenance = doc.pop("provenance", {}) or {}
    if not isinstance(provenance, dict):
        raise ValueError("provenance must be a mapping when present")
    source_stage = str(provenance.get("stage") or "generated").strip().lower()
    if source_stage not in ALLOWED_SOURCE_STAGES:
        allowed = ", ".join(sorted(ALLOWED_SOURCE_STAGES))
        raise ValueError(
            f"provenance.stage must be one of {allowed}; received {source_stage!r}"
        )

    metadata = {"provenance": provenance, **doc}
    normalized = {
        "id": record_id,
        "kind": kind,
        "title": title,
        "content_md": body,
        "refs": sorted(refs),
        "metadata": metadata,
        "source_stage": source_stage,
    }
    normalized_json = json.dumps(normalized, indent=2, sort_keys=True)
    content_hash = hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()
    return BundleEntry(
        record_id=record_id,
        kind=kind,
        title=title,
        content_md=body,
        refs=tuple(refs),
        metadata=metadata,
        source_stage=source_stage,
        source_path=source_path,
        content_hash=content_hash,
        normalized_json=normalized_json,
    )


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required string field {field_name!r}")
    return value.strip()


def _detect_secret_issues(entry: BundleEntry) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    payload = {
        "title": entry.title,
        "content_md": entry.content_md,
        "metadata": entry.metadata,
    }
    _scan_for_secrets(payload, entry.source_path, "root", issues)
    return issues


def _scan_for_secrets(
    value: Any, source_path: str, field_path: str, issues: list[ValidationIssue]
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            next_path = f"{field_path}.{key}"
            if (
                any(keyword in str(key).lower() for keyword in _SECRET_KEYWORDS)
                and isinstance(child, str)
                and child.strip().lower() not in _SAFE_SECRET_PLACEHOLDERS
            ):
                issues.append(
                    ValidationIssue(
                        path=source_path,
                        message=f"unsafe secret-like field {next_path}",
                    )
                )
            _scan_for_secrets(child, source_path, next_path, issues)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _scan_for_secrets(child, source_path, f"{field_path}[{index}]", issues)
        return
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            if pattern.search(value):
                issues.append(
                    ValidationIssue(
                        path=source_path,
                        message=f"unsafe secret-like value detected at {field_path}",
                    )
                )
                break


def _load_existing_records(duckdb_path: Path) -> dict[str, dict[str, str]]:
    con = open_duckdb_connection(duckdb_path, role="diagnostic")
    try:
        rows = con.execute("""
            SELECT record_id, content_hash, normalized_json
            FROM curated_context_records
            """).fetchall()
    finally:
        con.close()
    return {row[0]: {"content_hash": row[1], "normalized_json": row[2]} for row in rows}


def _changed_fields(current: dict[str, Any], proposed: dict[str, Any]) -> list[str]:
    keys = sorted(set(current) | set(proposed))
    return [key for key in keys if current.get(key) != proposed.get(key)]


def _insert_provenance_event(
    *,
    con: duckdb.DuckDBPyConnection,
    record_id: str,
    event_kind: str,
    source_path: str,
    content_hash: str,
    recorded_at: datetime,
    details_json: str,
) -> int:
    con.execute(
        """
        INSERT INTO curated_context_provenance (
          event_id,
          record_id,
          event_kind,
          source_path,
          content_hash,
          details_json,
          recorded_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            str(uuid4()),
            record_id,
            event_kind,
            source_path,
            content_hash,
            details_json,
            recorded_at,
        ],
    )
    return 1
