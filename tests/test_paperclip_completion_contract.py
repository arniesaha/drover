from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_paperclip_completion.py"
_SPEC = importlib.util.spec_from_file_location("check_paperclip_completion", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
validate_bundle = _MODULE.validate_bundle


def test_done_runtime_issue_requires_durable_evidence():
    findings = validate_bundle(
        {
            "items": [
                {
                    "paperclip_id": "AGE-50",
                    "paperclip_status": "done",
                    "github_issue": 158,
                    "github_state": "open",
                    "quality_required": True,
                    "runtime_change": True,
                    "state_mutation": True,
                    "pod_local_paths": ["/paperclip/home/workspace/nexus"],
                }
            ]
        }
    )

    messages = [f.message for f in findings]
    assert "done issue needs a durable artifact or no_pr_rationale" in messages
    assert "done issue needs validation output or not_tested_rationale" in messages
    assert "quality_required is true but quality_snapshot is missing" in messages
    assert "runtime_change is true but deployed_commit is missing" in messages
    assert "runtime_change is true but service_health is missing" in messages
    assert "state_mutation is true but no backup path is recorded" in messages
    assert any("non-closed GitHub issue" in msg for msg in messages)


def test_complete_runtime_issue_with_evidence_passes():
    findings = validate_bundle(
        {
            "items": [
                {
                    "paperclip_id": "AGE-50",
                    "paperclip_status": "done",
                    "github_issue": 158,
                    "github_state": "closed",
                    "artifacts": ["https://github.com/arniesaha/nexus/pull/169"],
                    "validation": ["uv run pytest"],
                    "quality_required": True,
                    "quality_snapshot": {
                        "status": "warn",
                        "score": 0.65,
                        "generated_at": "2026-06-20T15:56:48Z",
                    },
                    "runtime_change": True,
                    "deployed_commit": "5a32839",
                    "service_health": "com.nexus.server running",
                    "state_mutation": True,
                    "backups": [
                        "/Users/arnabmac/.nexus/backups/nexus.duckdb-pre-change.bak"
                    ],
                }
            ]
        }
    )

    assert findings == []


def test_review_and_blocked_items_need_explicit_next_state():
    findings = validate_bundle(
        {
            "items": [
                {"paperclip_id": "AGE-34", "paperclip_status": "in_review"},
                {"paperclip_id": "AGE-41", "paperclip_status": "blocked"},
            ]
        }
    )

    rendered = [f.render() for f in findings]
    assert any("AGE-34" in msg and "what is missing" in msg for msg in rendered)
    assert any("AGE-41" in msg and "blocked_on" in msg for msg in rendered)
