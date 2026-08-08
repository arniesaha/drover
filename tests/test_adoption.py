from __future__ import annotations

import json

from drover.server.adoption import adoption_snapshot, load_agent_adoption_registry
from drover.server.quality import _agent_adoption_category

AUDIT = {
    "repo_attribution": {
        "claude-laptop": {"total": 250, "project_total": 200},
        "codex-desktop": {"total": 180, "project_total": 150},
    },
    "latest_events": {},
}


def test_absent_adoption_configuration_is_neutral(monkeypatch) -> None:
    monkeypatch.delenv("DROVER_AGENT_ADOPTION_JSON", raising=False)

    snapshot = adoption_snapshot(AUDIT)
    category = _agent_adoption_category(AUDIT)

    assert snapshot["configured"] is False
    assert snapshot["runtimes"] == []
    assert snapshot["unmatched_high_volume_agent_ids"] == []
    assert snapshot["warnings"] == []
    assert category["status"] == "ok"
    assert category["warnings"] == []


def test_valid_adoption_configuration_drives_snapshot(monkeypatch) -> None:
    monkeypatch.setenv(
        "DROVER_AGENT_ADOPTION_JSON",
        json.dumps(
            [
                {
                    "runtime": "claude-fleet",
                    "agent_id_patterns": ["claude-*"],
                    "emits_to_drover": True,
                    "mcp_configured": True,
                    "drover_skill_configured": True,
                    "status": "active",
                    "smoke_check": "drover_data_quality",
                }
            ]
        ),
    )

    snapshot = adoption_snapshot(AUDIT)

    assert snapshot["configured"] is True
    assert snapshot["configuration_error"] is None
    assert snapshot["runtimes"][0]["observed_agent_ids"] == ["claude-laptop"]
    assert snapshot["runtimes"][0]["ready"] is True
    assert snapshot["unmatched_high_volume_agent_ids"] == ["codex-desktop"]
    assert snapshot["warnings"] == [
        "high-volume agents missing from adoption registry: codex-desktop"
    ]


def test_invalid_adoption_configuration_is_reported_without_raising(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DROVER_AGENT_ADOPTION_JSON", '{"runtime": "wrong-shape"}')

    registry = load_agent_adoption_registry()
    snapshot = adoption_snapshot(AUDIT)
    category = _agent_adoption_category(AUDIT)

    assert registry.configured is True
    assert registry.records == ()
    assert registry.error == "top-level value must be an array"
    assert snapshot["unmatched_high_volume_agent_ids"] == []
    assert snapshot["warnings"] == [
        "invalid DROVER_AGENT_ADOPTION_JSON: top-level value must be an array"
    ]
    assert category["status"] == "warn"


def test_invalid_record_fields_are_rejected() -> None:
    registry = load_agent_adoption_registry(
        '[{"runtime":"claude","agent_id_patterns":[],"emits_to_drover":true}]'
    )

    assert registry.configured is True
    assert registry.records == ()
    assert "agent_id_patterns" in (registry.error or "")
