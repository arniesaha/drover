"""Agent adoption registry and read-only adoption health helpers."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Iterable

HIGH_VOLUME_EVENT_THRESHOLD = 100


@dataclass(frozen=True)
class AgentAdoptionRecord:
    runtime: str
    agent_id_patterns: tuple[str, ...]
    emits_to_drover: bool
    mcp_configured: bool
    drover_skill_configured: bool
    status: str
    smoke_check: str

    @property
    def ready(self) -> bool:
        return (
            self.emits_to_drover
            and self.mcp_configured
            and self.drover_skill_configured
        )


DEFAULT_AGENT_ADOPTION: tuple[AgentAdoptionRecord, ...] = (
    AgentAdoptionRecord(
        runtime="mac-mini-max",
        agent_id_patterns=("macmini-claude", "max*", "max-v1"),
        emits_to_drover=True,
        mcp_configured=True,
        drover_skill_configured=True,
        status="active",
        smoke_check="drover_data_quality, drover_recent_sessions, drover_session_replay",
    ),
    AgentAdoptionRecord(
        runtime="openclaw-main",
        agent_id_patterns=("openclaw*", "nix*", "nix-v1"),
        emits_to_drover=True,
        mcp_configured=True,
        drover_skill_configured=True,
        status="active",
        smoke_check="drover_handoff, drover_project_brief, drover_data_quality",
    ),
    AgentAdoptionRecord(
        runtime="paperclip-agents",
        agent_id_patterns=("paperclip*", "claude_local", "codex_local"),
        emits_to_drover=True,
        mcp_configured=False,
        drover_skill_configured=True,
        status="needs-mcp-rollout",
        smoke_check="quality snapshot in completion evidence bundle",
    ),
    AgentAdoptionRecord(
        runtime="codex-cli",
        agent_id_patterns=("codex*", "codex_local"),
        emits_to_drover=True,
        mcp_configured=False,
        drover_skill_configured=True,
        status="needs-validation",
        smoke_check="drover_handoff and drover_data_quality before non-trivial work",
    ),
    AgentAdoptionRecord(
        runtime="work-macbook-claude",
        agent_id_patterns=("work-macbook*", "work-claude*"),
        emits_to_drover=True,
        mcp_configured=False,
        drover_skill_configured=False,
        status="data-source-only",
        smoke_check="shipper event freshness plus MCP setup check",
    ),
)


def _matches(patterns: Iterable[str], agent_id: str) -> bool:
    return any(fnmatch(agent_id, pattern) for pattern in patterns)


def adoption_snapshot(audit: dict[str, Any]) -> dict[str, Any]:
    """Return the static rollout matrix annotated with observed event volume."""

    attribution = audit.get("repo_attribution", {}) or {}
    latest_events = audit.get("latest_events", {}) or {}
    observed_agent_ids = set(attribution) | set(latest_events)
    totals = {
        agent_id: int((attribution.get(agent_id) or {}).get("total", 0) or 0)
        for agent_id in observed_agent_ids
    }
    project_totals = {
        agent_id: int(
            (attribution.get(agent_id) or {}).get(
                "project_total", (attribution.get(agent_id) or {}).get("total", 0)
            )
            or 0
        )
        for agent_id in observed_agent_ids
    }
    for agent_id in latest_events:
        totals.setdefault(agent_id, 1)

    runtimes: list[dict[str, Any]] = []
    matched_agents: set[str] = set()
    high_volume_unready: list[str] = []

    for record in DEFAULT_AGENT_ADOPTION:
        observed_ids = sorted(
            agent_id
            for agent_id in observed_agent_ids
            if _matches(record.agent_id_patterns, agent_id)
        )
        matched_agents.update(observed_ids)
        observed_events = sum(totals.get(agent_id, 0) for agent_id in observed_ids)
        observed_project_events = sum(
            project_totals.get(agent_id, 0) for agent_id in observed_ids
        )
        high_volume = observed_project_events >= HIGH_VOLUME_EVENT_THRESHOLD
        if high_volume and not record.ready:
            high_volume_unready.append(record.runtime)
        runtimes.append(
            {
                "runtime": record.runtime,
                "agent_id_patterns": list(record.agent_id_patterns),
                "observed_agent_ids": observed_ids,
                "observed_events": observed_events,
                "observed_project_events": observed_project_events,
                "emits_to_drover": record.emits_to_drover,
                "mcp_configured": record.mcp_configured,
                "drover_skill_configured": record.drover_skill_configured,
                "ready": record.ready,
                "status": record.status,
                "smoke_check": record.smoke_check,
                "high_volume": high_volume,
            }
        )

    unmatched_high_volume = sorted(
        agent_id
        for agent_id, total in project_totals.items()
        if agent_id not in matched_agents and total >= HIGH_VOLUME_EVENT_THRESHOLD
    )
    warnings: list[str] = []
    if high_volume_unready:
        warnings.append(
            "high-volume runtimes need Drover MCP/skill rollout: "
            + ", ".join(high_volume_unready)
        )
    if unmatched_high_volume:
        warnings.append(
            "high-volume agents missing from adoption registry: "
            + ", ".join(unmatched_high_volume)
        )

    return {
        "version": 1,
        "high_volume_event_threshold": HIGH_VOLUME_EVENT_THRESHOLD,
        "runtimes": runtimes,
        "observed_agent_ids": sorted(observed_agent_ids),
        "unmatched_high_volume_agent_ids": unmatched_high_volume,
        "high_volume_unready_runtimes": high_volume_unready,
        "warnings": warnings,
    }
