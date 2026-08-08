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


# Adoption records describe an operator's rollout, so Drover ships no
# machine-specific registry. Callers may supply their own records.
DEFAULT_AGENT_ADOPTION: tuple[AgentAdoptionRecord, ...] = ()


def _matches(patterns: Iterable[str], agent_id: str) -> bool:
    return any(fnmatch(agent_id, pattern) for pattern in patterns)


def adoption_snapshot(
    audit: dict[str, Any],
    records: Iterable[AgentAdoptionRecord] = DEFAULT_AGENT_ADOPTION,
) -> dict[str, Any]:
    """Return an operator-supplied rollout matrix with observed event volume."""

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

    for record in records:
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
