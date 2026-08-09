"""Stable, JSON-only instructions for advisory configuration analysis."""

from __future__ import annotations

import json

from drover.server.advisory.content_targets import ContentBundle

SYSTEM_PROMPT = """You review redacted configuration content for advisory findings.
Return exactly one JSON object with one top-level key, \"findings\". Do not use
Markdown or prose outside the JSON object. Each finding must contain exactly:
rule_id, target_id, severity, confidence, title, impact, evidence_excerpt, and
remediation. confidence must be \"likely\" or \"speculative\". remediation must
be an ordered array of bounded, operator-performed guidance. Never emit patches,
commands, mutation actions, or claims that Drover changed configuration. Quote
evidence only from the supplied redacted target. Return {\"findings\":[]} when
there is no useful finding."""


def build_analysis_request(bundle: ContentBundle) -> str:
    """Serialize only the already-redacted, bounded ephemeral bundle."""

    return json.dumps(
        {
            "host_id": bundle.host_id,
            "bundle_hash": bundle.bundle_hash,
            "targets": [
                {
                    "target_id": target.target_id,
                    "content_hash": target.content_hash,
                    "redacted_content": target.redacted_content,
                }
                for target in bundle.targets
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


__all__ = ["SYSTEM_PROMPT", "build_analysis_request"]
