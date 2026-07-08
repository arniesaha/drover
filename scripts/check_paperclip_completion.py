#!/usr/bin/env python3
"""Validate Drover/Paperclip completion evidence bundles.

This script is deliberately offline. Export Paperclip issue state, GitHub issue
state, and repo/runtime evidence into a small JSON file, then run this checker
before accepting a Paperclip issue as done.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DONE_STATUSES = {"done", "closed", "complete", "completed"}
REVIEW_STATUSES = {"in_review", "review", "ready_for_review"}


@dataclass
class Finding:
    level: str
    paperclip_id: str
    message: str

    def render(self) -> str:
        return f"{self.level.upper()} {self.paperclip_id}: {self.message}"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_any_text(values: Any) -> bool:
    return any(_has_text(v) for v in _as_list(values))


def _status(value: Any) -> str:
    return str(value or "").strip().lower()


def validate_item(item: dict[str, Any]) -> list[Finding]:
    paperclip_id = str(item.get("paperclip_id") or item.get("paperclip") or "<unknown>")
    findings: list[Finding] = []
    paperclip_status = _status(item.get("paperclip_status"))
    github_state = _status(item.get("github_state"))
    github_issue = item.get("github_issue")

    if not _has_text(paperclip_id) or paperclip_id == "<unknown>":
        findings.append(Finding("error", paperclip_id, "missing paperclip_id"))
    if not paperclip_status:
        findings.append(Finding("error", paperclip_id, "missing paperclip_status"))

    is_done = paperclip_status in DONE_STATUSES
    is_review = paperclip_status in REVIEW_STATUSES

    if is_done:
        has_artifact = _has_any_text(item.get("artifacts")) or _has_text(
            item.get("no_pr_rationale")
        )
        has_validation = _has_any_text(item.get("validation")) or _has_text(
            item.get("not_tested_rationale")
        )

        if github_issue is not None and github_state != "closed":
            if not _has_text(item.get("github_open_rationale")):
                findings.append(
                    Finding(
                        "error",
                        paperclip_id,
                        "done issue links to a non-closed GitHub issue without github_open_rationale",
                    )
                )
        if not has_artifact:
            findings.append(
                Finding(
                    "error",
                    paperclip_id,
                    "done issue needs a durable artifact or no_pr_rationale",
                )
            )
        if not has_validation:
            findings.append(
                Finding(
                    "error",
                    paperclip_id,
                    "done issue needs validation output or not_tested_rationale",
                )
            )
        if item.get("quality_required") and not isinstance(
            item.get("quality_snapshot"), dict
        ):
            findings.append(
                Finding(
                    "error",
                    paperclip_id,
                    "quality_required is true but quality_snapshot is missing",
                )
            )
        if item.get("runtime_change"):
            if not _has_text(item.get("deployed_commit")):
                findings.append(
                    Finding(
                        "error",
                        paperclip_id,
                        "runtime_change is true but deployed_commit is missing",
                    )
                )
            if not _has_text(item.get("service_health")):
                findings.append(
                    Finding(
                        "error",
                        paperclip_id,
                        "runtime_change is true but service_health is missing",
                    )
                )
        if item.get("state_mutation") and not _has_any_text(item.get("backups")):
            findings.append(
                Finding(
                    "error",
                    paperclip_id,
                    "state_mutation is true but no backup path is recorded",
                )
            )

    if is_review and not _has_text(item.get("review_missing")):
        findings.append(
            Finding(
                "warning",
                paperclip_id,
                "in_review issue should state what is missing before done",
            )
        )

    if paperclip_status == "blocked" and not _has_text(item.get("blocked_on")):
        findings.append(
            Finding("error", paperclip_id, "blocked issue needs blocked_on")
        )

    if _has_any_text(item.get("pod_local_paths")) and not _has_any_text(
        item.get("artifacts")
    ):
        findings.append(
            Finding(
                "warning",
                paperclip_id,
                "pod-local paths are recovery evidence, not durable completion artifacts",
            )
        )

    return findings


def validate_bundle(bundle: dict[str, Any]) -> list[Finding]:
    raw_items = bundle.get("items")
    if not isinstance(raw_items, list):
        return [Finding("error", "<bundle>", "top-level 'items' must be a list")]

    findings: list[Finding] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            findings.append(Finding("error", "<bundle>", "each item must be an object"))
            continue
        findings.extend(validate_item(raw))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="Evidence JSON file")
    parser.add_argument(
        "--warnings-as-errors",
        action="store_true",
        help="Exit non-zero when warnings are present",
    )
    args = parser.parse_args(argv)

    try:
        bundle = json.loads(args.evidence.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR <bundle>: failed to read evidence JSON: {exc}", file=sys.stderr)
        return 2

    findings = validate_bundle(bundle)
    for finding in findings:
        print(finding.render())

    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warning"]
    if errors or (args.warnings_as_errors and warnings):
        return 1

    print(f"OK: {len(bundle.get('items', []))} completion item(s) validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
