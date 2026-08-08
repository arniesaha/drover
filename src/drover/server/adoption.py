"""Agent adoption registry and read-only adoption health helpers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Iterable

HIGH_VOLUME_EVENT_THRESHOLD = 100
AGENT_ADOPTION_ENV = "DROVER_AGENT_ADOPTION_JSON"


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


@dataclass(frozen=True)
class AgentAdoptionRegistry:
    configured: bool
    records: tuple[AgentAdoptionRecord, ...] = ()
    error: str | None = None


def load_agent_adoption_registry(raw: str | None = None) -> AgentAdoptionRegistry:
    """Load the optional operator adoption registry.

    The environment value is a JSON array of records. Configuration is loaded
    when a snapshot is requested rather than at import time, which keeps tests,
    CLI invocations, and long-running processes predictable. Invalid input is
    reported as data instead of raising from the quality or observatory paths.
    """
    value = os.environ.get(AGENT_ADOPTION_ENV, "") if raw is None else raw
    value = value.strip()
    if not value:
        return AgentAdoptionRegistry(configured=False)
    try:
        entries = json.loads(value)
    except json.JSONDecodeError as exc:
        return AgentAdoptionRegistry(
            configured=True,
            error=f"invalid JSON at character {exc.pos}",
        )
    if not isinstance(entries, list):
        return AgentAdoptionRegistry(
            configured=True,
            error="top-level value must be an array",
        )

    records: list[AgentAdoptionRecord] = []
    required_booleans = (
        "emits_to_drover",
        "mcp_configured",
        "drover_skill_configured",
    )
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return AgentAdoptionRegistry(
                configured=True,
                error=f"entry {index} must be an object",
            )
        runtime = entry.get("runtime")
        patterns = entry.get("agent_id_patterns")
        if not isinstance(runtime, str) or not runtime.strip():
            return AgentAdoptionRegistry(
                configured=True,
                error=f"entry {index}.runtime must be a non-empty string",
            )
        if (
            not isinstance(patterns, list)
            or not patterns
            or any(not isinstance(pattern, str) or not pattern for pattern in patterns)
        ):
            return AgentAdoptionRegistry(
                configured=True,
                error=(
                    f"entry {index}.agent_id_patterns must be a non-empty "
                    "array of strings"
                ),
            )
        for field in required_booleans:
            if not isinstance(entry.get(field), bool):
                return AgentAdoptionRegistry(
                    configured=True,
                    error=f"entry {index}.{field} must be a boolean",
                )
        status = entry.get("status", "unknown")
        smoke_check = entry.get("smoke_check", "")
        if not isinstance(status, str) or not isinstance(smoke_check, str):
            return AgentAdoptionRegistry(
                configured=True,
                error=f"entry {index}.status and smoke_check must be strings",
            )
        records.append(
            AgentAdoptionRecord(
                runtime=runtime.strip(),
                agent_id_patterns=tuple(patterns),
                emits_to_drover=entry["emits_to_drover"],
                mcp_configured=entry["mcp_configured"],
                drover_skill_configured=entry["drover_skill_configured"],
                status=status,
                smoke_check=smoke_check,
            )
        )
    return AgentAdoptionRegistry(configured=True, records=tuple(records))


def _matches(patterns: Iterable[str], agent_id: str) -> bool:
    return any(fnmatch(agent_id, pattern) for pattern in patterns)


def adoption_snapshot(
    audit: dict[str, Any],
    records: Iterable[AgentAdoptionRecord] | None = None,
) -> dict[str, Any]:
    """Return an operator-supplied rollout matrix with observed event volume."""

    registry = (
        load_agent_adoption_registry()
        if records is None
        else AgentAdoptionRegistry(configured=True, records=tuple(records))
    )

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

    for record in registry.records:
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

    unmatched_high_volume = (
        sorted(
            agent_id
            for agent_id, total in project_totals.items()
            if agent_id not in matched_agents and total >= HIGH_VOLUME_EVENT_THRESHOLD
        )
        if registry.configured and registry.error is None
        else []
    )
    warnings: list[str] = []
    if registry.error:
        warnings.append(f"invalid {AGENT_ADOPTION_ENV}: {registry.error}")
    elif high_volume_unready:
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
        "configured": registry.configured,
        "configuration_error": registry.error,
        "high_volume_event_threshold": HIGH_VOLUME_EVENT_THRESHOLD,
        "runtimes": runtimes,
        "observed_agent_ids": sorted(observed_agent_ids),
        "unmatched_high_volume_agent_ids": unmatched_high_volume,
        "high_volume_unready_runtimes": high_volume_unready,
        "warnings": warnings,
    }
