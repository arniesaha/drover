"""Structured Drover lakehouse data-quality snapshot."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from drover.server.adoption import adoption_snapshot
from drover.server.doctor import RUNTIME_KEY_RELATIONS, runtime_audit

FRESH_EVENT_WARN_HOURS = 6
FRESH_EVENT_CRITICAL_HOURS = 24
FRESH_SPAN_WARN_HOURS = 6
FRESH_SPAN_CRITICAL_HOURS = 24
ATTRIBUTION_WARN_PERCENT = 90.0
ATTRIBUTION_CRITICAL_MIN_EVENTS = 100
SUMMARY_COVERAGE_CRITICAL_PERCENT = 95.0

_STATUS_SCORE = {"ok": 1.0, "warn": 0.5, "critical": 0.0, "unknown": 0.0}
_STATUS_RANK = {"ok": 0, "warn": 1, "critical": 2, "unknown": 2}


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = datetime.fromisoformat(text.replace(" ", "T", 1))
            except ValueError:
                return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _age_hours(value: object, *, now: datetime) -> float | None:
    ts = _parse_timestamp(value)
    if ts is None:
        return None
    return round(max((now - ts).total_seconds(), 0.0) / 3600.0, 3)


def _worst_status(*statuses: str) -> str:
    if not statuses:
        return "unknown"
    return max(statuses, key=lambda status: _STATUS_RANK.get(status, 2))


def _category(status: str, details: dict, warnings: list[str] | None = None) -> dict:
    return {
        "status": status,
        "score": _STATUS_SCORE.get(status, 0.0),
        "details": details,
        "warnings": warnings or [],
    }


def _agent_id_set(values: Iterable[str] | None) -> set[str]:
    return {str(value).strip() for value in values or [] if str(value).strip()}


def _required_agent_ids(values: Iterable[str] | None = None) -> set[str]:
    required = _agent_id_set(values)
    env_value = os.environ.get("DROVER_QUALITY_REQUIRED_AGENTS") or ""
    if env_value:
        required.update(
            part.strip()
            for part in env_value.replace(";", ",").split(",")
            if part.strip()
        )
    return required


def _freshness_category(
    audit: dict, *, now: datetime, required_agent_ids: Iterable[str] | None = None
) -> dict:
    required_agents = _required_agent_ids(required_agent_ids)
    event_ages = {
        agent_id: _age_hours(row.get("timestamp"), now=now)
        for agent_id, row in audit.get("latest_events", {}).items()
    }
    known_event_ages = {
        agent_id: age for agent_id, age in event_ages.items() if age is not None
    }
    oldest_event_agent = None
    oldest_latest_event_age = None
    freshest_latest_event_age = None
    if known_event_ages:
        oldest_event_agent, oldest_latest_event_age = max(
            known_event_ages.items(), key=lambda item: item[1]
        )
        freshest_latest_event_age = min(known_event_ages.values())
    latest_span_age = _age_hours(
        audit.get("span_health", {}).get("latest_start"), now=now
    )
    unprocessed = len(audit.get("unprocessed_incoming", []))
    statuses = []
    warnings: list[str] = []

    if oldest_latest_event_age is None:
        statuses.append("critical")
        warnings.append("no recent agent_events found")
    else:
        missing_required_agents = sorted(required_agents - set(event_ages))
        stale_required_agents = sorted(
            agent_id
            for agent_id in required_agents
            if (age := known_event_ages.get(agent_id)) is not None
            and age > FRESH_EVENT_CRITICAL_HOURS
        )
        if missing_required_agents:
            statuses.append("critical")
            warnings.append(
                "missing required source latest event: "
                + ", ".join(missing_required_agents)
            )
        if stale_required_agents:
            statuses.append("critical")
            warnings.extend(
                "stale required source latest event: "
                f"{agent_id}={known_event_ages[agent_id]:.1f}h old"
                for agent_id in stale_required_agents
            )
    if (
        oldest_latest_event_age is not None
        and oldest_latest_event_age > FRESH_EVENT_CRITICAL_HOURS
    ):
        if oldest_event_agent in required_agents:
            if not any(
                warning.startswith(
                    f"stale required source latest event: {oldest_event_agent}="
                )
                for warning in warnings
            ):
                statuses.append("critical")
                warnings.append(
                    "stale required source latest event: "
                    f"{oldest_event_agent}={oldest_latest_event_age:.1f}h old"
                )
        else:
            statuses.append("warn")
            warnings.append(
                "stale idle source latest event: "
                f"{oldest_event_agent}={oldest_latest_event_age:.1f}h old"
            )
    elif (
        oldest_latest_event_age is not None
        and oldest_latest_event_age > FRESH_EVENT_WARN_HOURS
    ):
        statuses.append("warn")
        warnings.append(
            "aging source latest event: "
            f"{oldest_event_agent}={oldest_latest_event_age:.1f}h old"
        )

    if latest_span_age is None:
        statuses.append("warn")
        warnings.append("no recent spans found")
    elif latest_span_age > FRESH_SPAN_CRITICAL_HOURS:
        statuses.append("critical")
        warnings.append(f"latest span is {latest_span_age:.1f}h old")
    elif latest_span_age > FRESH_SPAN_WARN_HOURS:
        statuses.append("warn")

    if unprocessed:
        statuses.append("warn")
        warnings.append(f"{unprocessed} incoming jsonl files are unprocessed")

    return _category(
        _worst_status(*statuses, "ok"),
        {
            "latest_event_age_hours": oldest_latest_event_age,
            "latest_event_age_hours_by_agent": event_ages,
            "oldest_latest_event_age_hours": oldest_latest_event_age,
            "oldest_latest_event_agent_id": oldest_event_agent,
            "freshest_latest_event_age_hours": freshest_latest_event_age,
            "latest_span_age_hours": latest_span_age,
            "unprocessed_incoming_files": unprocessed,
            "span_recent_count": audit.get("span_health", {}).get("recent_count"),
            "required_agent_ids": sorted(required_agents),
        },
        warnings,
    )


def _completeness_category(audit: dict) -> dict:
    counts = audit.get("table_counts", {})
    missing = [name for name in RUNTIME_KEY_RELATIONS if counts.get(name) is None]
    empty_core = [name for name in ("agent_events", "spans") if counts.get(name) == 0]
    summarize_counts = audit.get("summarize_jobs", {}).get("status_counts", {})
    summarize_health = audit.get("summarize_jobs", {}).get("backend_health", {})
    errored_summaries = int(summarize_counts.get("errored", 0))
    warnings: list[str] = []
    status = "ok"
    if missing or empty_core:
        status = "critical"
    elif errored_summaries:
        status = "warn"
    if missing:
        warnings.append(f"missing relations: {', '.join(missing)}")
    if empty_core:
        warnings.append(f"empty core relations: {', '.join(empty_core)}")
    if errored_summaries:
        warnings.append(f"{errored_summaries} summarize_jobs are errored")
    return _category(
        status,
        {
            "table_counts": counts,
            "missing_relations": missing,
            "empty_core_relations": empty_core,
            "errored_summarize_jobs": errored_summaries,
            "summarizer_backend_health": summarize_health,
        },
        warnings,
    )


def _attribution_category(audit: dict) -> dict:
    attribution = audit.get("repo_attribution", {})
    percents = {
        agent_id: float(row.get("percent", 0.0))
        for agent_id, row in attribution.items()
    }
    totals = {
        agent_id: int(row.get("total", 0)) for agent_id, row in attribution.items()
    }
    general_workspace_by_agent = {
        agent_id: int(row.get("general_workspace", 0))
        for agent_id, row in attribution.items()
    }
    project_totals = {
        agent_id: int(row.get("project_total", totals.get(agent_id, 0)))
        for agent_id, row in attribution.items()
    }
    attributed_by_agent = {
        agent_id: int(row.get("attributed", 0)) for agent_id, row in attribution.items()
    }
    total_events = sum(totals.values())
    project_events = sum(project_totals.values())
    general_workspace_events = sum(general_workspace_by_agent.values())
    attributed_events = sum(attributed_by_agent.values())
    min_percent = min(percents.values(), default=None)
    high_volume_zero_agents = sorted(
        agent_id
        for agent_id, attributed in attributed_by_agent.items()
        if attributed == 0
        and project_totals.get(agent_id, 0) >= ATTRIBUTION_CRITICAL_MIN_EVENTS
    )
    low_volume_zero_agents = sorted(
        agent_id
        for agent_id, attributed in attributed_by_agent.items()
        if attributed == 0
        and 0 < project_totals.get(agent_id, 0) < ATTRIBUTION_CRITICAL_MIN_EVENTS
    )
    repo_attribution_skipped = not attribution and any(
        "repo attribution" in str(check) for check in audit.get("skipped_checks", [])
    )
    if repo_attribution_skipped:
        status = "ok"
        warnings = []
    elif not attribution:
        status = "unknown"
        warnings = ["repo attribution unavailable"]
    elif high_volume_zero_agents:
        status = "critical"
        warnings = [
            "high-volume agents have 0% repo attribution: "
            + ", ".join(
                f"{agent_id} ({totals.get(agent_id, 0)} events)"
                for agent_id in high_volume_zero_agents
            )
        ]
    elif low_volume_zero_agents:
        status = "warn"
        warnings = [
            "low-volume agents have 0% repo attribution: "
            + ", ".join(
                f"{agent_id} ({totals.get(agent_id, 0)} events)"
                for agent_id in low_volume_zero_agents
            )
        ]
    elif min_percent is not None and min_percent < ATTRIBUTION_WARN_PERCENT:
        status = "warn"
        warnings = [
            f"minimum repo attribution is {min_percent:.1f}% "
            f"(threshold {ATTRIBUTION_WARN_PERCENT:.1f}%)"
        ]
    else:
        status = "ok"
        warnings = []
    return _category(
        status,
        {
            "repo_attribution_percent_by_agent": percents,
            "repo_attribution_total_by_agent": totals,
            "repo_attribution_project_total_by_agent": project_totals,
            "repo_attribution_general_workspace_by_agent": general_workspace_by_agent,
            "repo_attribution_attributed_by_agent": attributed_by_agent,
            "minimum_repo_attribution_percent": min_percent,
            "attributed_events": attributed_events,
            "total_events": total_events,
            "project_events": project_events,
            "general_workspace_events": general_workspace_events,
            "critical_min_events": ATTRIBUTION_CRITICAL_MIN_EVENTS,
            "high_volume_zero_attribution_agents": high_volume_zero_agents,
            "low_volume_zero_attribution_agents": low_volume_zero_agents,
            "repo_attribution_skipped": repo_attribution_skipped,
            "repo_attribution_evaluation": (
                "skipped" if repo_attribution_skipped else "evaluated"
            ),
            "repo_attribution_skip_reason": (
                "standard diagnostic mode" if repo_attribution_skipped else None
            ),
            "top_unattributed_cwds": audit.get("top_unattributed_cwds", {}),
            "general_workspace_cwds": audit.get("general_workspace_cwds", {}),
        },
        warnings,
    )


def _identity_category(audit: dict) -> dict:
    identity = audit.get("agent_event_identity", {})
    duplicate_ids = int(identity.get("duplicate_id_values", 0) or 0)
    duplicate_dedup_keys = int(identity.get("duplicate_dedup_key_values", 0) or 0)
    if identity.get("status") == "missing":
        status = "unknown"
        warnings = ["agent_event identity audit unavailable"]
    elif duplicate_dedup_keys:
        status = "critical"
        warnings = ["duplicate dedup_key values break canonical event identity"]
    elif duplicate_ids:
        status = "ok"
        warnings = []
    else:
        status = "ok"
        warnings = []
    return _category(
        status,
        {
            "canonical_semantics": identity.get("canonical_semantics", "dedup_key"),
            "source_id_context": identity.get(
                "source_id_context", "source/provenance only; not canonical health"
            ),
            "duplicate_id_values": duplicate_ids,
            "duplicate_id_rows": int(identity.get("duplicate_id_rows", 0) or 0),
            "duplicate_dedup_key_values": duplicate_dedup_keys,
            "duplicate_dedup_key_rows": int(
                identity.get("duplicate_dedup_key_rows", 0) or 0
            ),
        },
        warnings,
    )


def _derived_context_category(audit: dict) -> dict:
    summarize_counts = audit.get("summarize_jobs", {}).get("status_counts", {})
    summarize_health = audit.get("summarize_jobs", {}).get("backend_health", {})
    embed_counts = audit.get("embed_jobs", {}).get("status_counts", {})
    embeddings = audit.get("session_embeddings_count")
    summaries = audit.get("table_counts", {}).get("session_summaries")
    embedding_state = audit.get("embedding_status", {}).get("state", "unknown")
    session_consistency = audit.get("session_consistency", {})
    pending_embed = int(embed_counts.get("pending", 0) or 0)
    errored_embed = int(embed_counts.get("errored", 0) or 0)
    pending_summary = int(summarize_counts.get("pending", 0) or 0)
    errored_summary = int(summarize_counts.get("errored", 0) or 0)
    handoff_ready = int(bool((summaries or 0) > 0 and (embeddings or 0) > 0))

    statuses = ["ok"]
    warnings: list[str] = []
    if not handoff_ready:
        statuses.append("critical")
        warnings.append("handoff context is not ready: summaries or embeddings missing")
    if embedding_state in {"offline_or_unconfigured", "errors"}:
        statuses.append("critical")
        warnings.append(audit.get("embedding_status", {}).get("message", ""))
    elif pending_embed or pending_summary or errored_embed or errored_summary:
        statuses.append("warn")
    if session_consistency.get("status") not in {"ok", "missing"}:
        statuses.append("warn")
        warnings.append(
            f"session consistency status is {session_consistency.get('status')}"
        )

    return _category(
        _worst_status(*statuses),
        {
            "handoff_ready": handoff_ready,
            "session_summaries": summaries,
            "session_embeddings": embeddings,
            "summarize_job_status_counts": summarize_counts,
            "embed_job_status_counts": embed_counts,
            "embedding_status": embedding_state,
            "pending_summarize_jobs": pending_summary,
            "errored_summarize_jobs": errored_summary,
            "summarizer_backend_health": summarize_health,
            "pending_embed_jobs": pending_embed,
            "errored_embed_jobs": errored_embed,
            "session_consistency_status": session_consistency.get("status"),
        },
        [warning for warning in warnings if warning],
    )


def _summary_coverage_category(audit: dict) -> dict:
    session_consistency = audit.get("session_consistency", {})
    summarize_counts = audit.get("summarize_jobs", {}).get("status_counts", {})
    summarize_health = audit.get("summarize_jobs", {}).get("backend_health", {})
    event_sessions = session_consistency.get("event_sessions")
    missing_summaries = session_consistency.get("event_sessions_without_summary")
    orphan_summaries = session_consistency.get("summaries_without_events")
    pending_summary = int(summarize_counts.get("pending", 0) or 0)
    errored_summary = int(summarize_counts.get("errored", 0) or 0)
    retryable_summary = int(summarize_health.get("retryable_errors", 0) or 0)
    non_retryable_summary = int(summarize_health.get("non_retryable_errors", 0) or 0)

    coverage_percent = None
    if event_sessions and missing_summaries is not None:
        covered = max(int(event_sessions) - int(missing_summaries), 0)
        coverage_percent = round((covered / int(event_sessions)) * 100.0, 1)

    if session_consistency.get("status") == "missing":
        return _category(
            "unknown",
            {
                "event_sessions": event_sessions,
                "event_sessions_without_summary": missing_summaries,
                "summaries_without_events": orphan_summaries,
                "coverage_percent": coverage_percent,
                "pending_summarize_jobs": pending_summary,
                "errored_summarize_jobs": errored_summary,
                "retryable_summarize_errors": retryable_summary,
                "non_retryable_summarize_errors": non_retryable_summary,
            },
            ["summary coverage unavailable"],
        )

    statuses = ["ok"]
    warnings: list[str] = []
    if event_sessions and missing_summaries:
        if pending_summary or retryable_summary:
            statuses.append("critical")
        elif (
            coverage_percent is not None
            and coverage_percent < SUMMARY_COVERAGE_CRITICAL_PERCENT
        ):
            statuses.append("critical")
        else:
            statuses.append("warn")
        warnings.append(
            f"{missing_summaries} event sessions do not have a generated summary"
        )
    if orphan_summaries:
        statuses.append("warn")
        warnings.append(f"{orphan_summaries} summaries do not map to event sessions")
    if retryable_summary:
        statuses.append("critical")
        warnings.append(f"{retryable_summary} summarize_jobs have retryable errors")
    elif errored_summary:
        statuses.append("warn")
        warnings.append(f"{errored_summary} summarize_jobs are terminally errored")
    if pending_summary:
        statuses.append("critical")
        warnings.append(f"{pending_summary} summarize_jobs are still pending")

    return _category(
        _worst_status(*statuses),
        {
            "event_sessions": event_sessions,
            "event_sessions_without_summary": missing_summaries,
            "summaries_without_events": orphan_summaries,
            "coverage_percent": coverage_percent,
            "pending_summarize_jobs": pending_summary,
            "errored_summarize_jobs": errored_summary,
            "retryable_summarize_errors": retryable_summary,
            "non_retryable_summarize_errors": non_retryable_summary,
            "session_consistency_status": session_consistency.get("status"),
        },
        warnings,
    )


def _embedding_coverage_category(audit: dict) -> dict:
    summaries = audit.get("table_counts", {}).get("session_summaries")
    session_embeddings = audit.get("session_embeddings_count")
    embed_counts = audit.get("embed_jobs", {}).get("status_counts", {})
    span_coverage = audit.get("span_embedding_coverage", {})
    embedding_status = audit.get("embedding_status", {})
    pending_embed = int(embed_counts.get("pending", 0) or 0)
    errored_embed = int(embed_counts.get("errored", 0) or 0)

    session_coverage_percent = None
    if summaries:
        session_coverage_percent = round(
            (int(session_embeddings or 0) / int(summaries)) * 100.0, 1
        )
        session_coverage_percent = min(100.0, session_coverage_percent)

    statuses = ["ok"]
    warnings: list[str] = []
    state = embedding_status.get("state", "unknown")
    message = embedding_status.get("message", "")
    if summaries and int(session_embeddings or 0) == 0:
        statuses.append("critical")
        warnings.append("session summary embeddings are missing")
    elif summaries and int(session_embeddings or 0) < int(summaries):
        statuses.append("warn")
        warnings.append("session summary embedding coverage is partial")

    if state in {"offline_or_unconfigured", "errors"}:
        statuses.append("critical")
        warnings.append(message)
    elif pending_embed or errored_embed:
        statuses.append("warn")
        if pending_embed:
            warnings.append(f"{pending_embed} embed_jobs are still pending")
        if errored_embed:
            warnings.append(f"{errored_embed} embed_jobs are errored")

    span_percent = span_coverage.get("coverage_percent")
    if span_percent is not None and float(span_percent) < 100.0:
        statuses.append("warn")
        warnings.append(f"span embedding coverage is {span_percent:.1f}%")
    if int(span_coverage.get("stale_running_jobs", 0) or 0):
        statuses.append("warn")
        warnings.append(
            f"{span_coverage.get('stale_running_jobs', 0)} span_embed_jobs are stale-running"
        )

    return _category(
        _worst_status(*statuses),
        {
            "session_summaries": summaries,
            "session_embeddings": session_embeddings,
            "session_embedding_coverage_percent": session_coverage_percent,
            "embed_job_status_counts": embed_counts,
            "embedding_status": state,
            "embedding_status_message": message,
            "span_embedding_coverage": span_coverage,
        },
        [warning for warning in warnings if warning],
    )


def _span_linkability_category(audit: dict) -> dict | None:
    health = audit.get("openclaw_agentweave_health") or {}
    linkability = health.get("linkability") or {}
    status = health.get("status")
    total = int(linkability.get("openclaw_like_spans", 0) or 0)
    unmatched = int(linkability.get("unmatched_spans", 0) or 0)
    matched = int(linkability.get("matched_spans", 0) or 0)
    coverage_percent = None
    if total:
        coverage_percent = round((matched / total) * 100.0, 1)

    if not status or status == "missing":
        return None

    warnings: list[str] = []
    category_status = "ok"
    if unmatched:
        category_status = "warn"
        warnings.append(f"{unmatched} spans are not linkable to native sessions")
    return _category(
        category_status,
        {
            "matched_spans": matched,
            "unmatched_spans": unmatched,
            "openclaw_like_spans": total,
            "exact_session_id_matches": int(
                linkability.get("exact_session_id_matches", 0) or 0
            ),
            "session_key_matches": int(linkability.get("session_key_matches", 0) or 0),
            "coverage_percent": coverage_percent,
        },
        warnings,
    )


def _bundle_quality_category(audit: dict) -> dict:
    bundle = audit.get("bundle_quality", {})
    total = bundle.get("total_summaries")
    # ``bundle_ready_*`` is intentionally the stricter rich-evidence metadata
    # gate (prompt excerpts, assistant excerpt, files/open questions).  Older
    # generated summaries can still be useful for recall when they have summary
    # text, a completed status, and an embedding.  Quality should alert on the
    # latter processing gap, not on absent optional historical evidence.
    usable = bundle.get("recall_usable_summaries")
    if usable is None:
        # Backward compatibility for callers/tests that provide pre-#238 audit
        # dictionaries without the recall/rich split.
        usable = bundle.get("bundle_ready_summaries")

    if total is None:
        return _category("unknown", bundle, ["bundle quality unavailable"])

    total_int = int(total or 0)
    usable_int = int(usable or 0)
    statuses = ["ok"]
    warnings: list[str] = []
    if total_int == 0:
        statuses.append("critical")
        warnings.append("no generated session bundles are available")
    elif usable_int == 0:
        statuses.append("critical")
        warnings.append(
            "generated session bundles are not usable for recall: summary text, completed status, or embeddings are missing"
        )
    elif usable_int < total_int:
        statuses.append("warn")
        warnings.append(
            f"recall-usable summaries cover {usable_int}/{total_int} generated bundles; missing summary text, completed status, or embeddings"
        )

    return _category(
        _worst_status(*statuses),
        bundle,
        warnings,
    )


def _openclaw_agentweave_category(audit: dict) -> dict | None:
    health = audit.get("openclaw_agentweave_health") or {}
    status = health.get("status")
    if not status or status == "missing":
        return None

    native = health.get("native_events", {})
    spans = health.get("spans", {})
    linkability = health.get("linkability", {})
    warnings: list[str] = []

    if native.get("total", 0) and native.get("raw_harness_openclaw", 0) == 0:
        warnings.append(
            "OpenClaw/AgentWeave native events are flowing but raw OpenClaw contract fields are absent"
        )
    if spans.get("attr_openclaw_but_column_harness_null", 0):
        warnings.append(
            "OpenClaw/AgentWeave span attrs are present but not promoted to durable columns"
        )
    if linkability.get("unmatched_spans", 0):
        warnings.append(
            "OpenClaw/AgentWeave spans are not linkable to native OpenClaw sessions"
        )

    return _category(
        str(status),
        {
            "native_events": native,
            "spans": spans,
            "linkability": linkability,
        },
        warnings,
    )


def _agent_adoption_category(audit: dict) -> dict:
    adoption = adoption_snapshot(audit)
    warnings = list(adoption.get("warnings", []))
    status = "warn" if warnings else "ok"
    return _category(status, adoption, warnings)


def quality_snapshot(
    *,
    duckdb_path: Path,
    incoming_dir: Optional[Path] = None,
    hours: int = 24,
    now: datetime | None = None,
    required_agent_ids: Iterable[str] | None = None,
    deep: bool = True,
) -> dict:
    """Return a structured Drover data-quality snapshot.

    The snapshot is derived from ``runtime_audit`` so the CLI, automation, and
    Grafana metrics all use the same read-only lakehouse health signals.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    audit = runtime_audit(
        duckdb_path=duckdb_path, incoming_dir=incoming_dir, hours=hours, deep=deep
    )
    categories = {
        "freshness": _freshness_category(
            audit, now=now, required_agent_ids=required_agent_ids
        ),
        "completeness": _completeness_category(audit),
        "summary_coverage": _summary_coverage_category(audit),
        "embedding_coverage": _embedding_coverage_category(audit),
        "attribution": _attribution_category(audit),
        "bundle_quality": _bundle_quality_category(audit),
        "identity": _identity_category(audit),
        "derived_context": _derived_context_category(audit),
        "agent_adoption": _agent_adoption_category(audit),
    }
    span_linkability = _span_linkability_category(audit)
    if span_linkability is not None:
        categories["span_linkability"] = span_linkability
    openclaw_category = _openclaw_agentweave_category(audit)
    if openclaw_category is not None:
        categories["openclaw_agentweave"] = openclaw_category
    status = _worst_status(*(category["status"] for category in categories.values()))
    score = round(
        sum(float(category["score"]) for category in categories.values())
        / len(categories),
        3,
    )
    warnings = list(audit.get("warnings", []))
    for name, category in categories.items():
        for warning in category.get("warnings", []):
            warnings.append(f"{name}: {warning}")
    return {
        "snapshot_version": 1,
        "generated_at": now.isoformat(),
        "duckdb_path": str(duckdb_path),
        "incoming_dir": str(incoming_dir) if incoming_dir else None,
        "hours": hours,
        "diagnostic_depth": "deep" if deep else "standard",
        "status": status,
        "score": score,
        "categories": categories,
        "warnings": warnings,
        "runtime_audit": audit,
    }


def _label_value(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: object) -> str:
    if not labels:
        return ""
    rendered = ",".join(
        f'{key}="{_label_value(value)}"' for key, value in labels.items()
    )
    return f"{{{rendered}}}"


def _metric(name: str, value: object, **labels: object) -> str:
    if isinstance(value, bool):
        metric_value = 1 if value else 0
    elif value is None:
        metric_value = 0
    else:
        metric_value = value
    return f"{name}{_labels(**labels)} {metric_value}"


def format_prometheus(snapshot: dict) -> str:
    """Render a quality snapshot as Prometheus text exposition."""
    lines = [
        "# HELP drover_quality_score Drover data-quality score from 0 to 1.",
        "# TYPE drover_quality_score gauge",
        _metric("drover_quality_score", snapshot.get("score", 0), category="overall"),
    ]
    for name, category in snapshot.get("categories", {}).items():
        lines.append(
            _metric("drover_quality_score", category.get("score", 0), category=name)
        )

    lines.extend(
        [
            "# HELP drover_quality_status Drover data-quality status by category.",
            "# TYPE drover_quality_status gauge",
        ]
    )
    statuses = ("ok", "warn", "critical", "unknown")
    overall_status = snapshot.get("status", "unknown")
    for status in statuses:
        lines.append(
            _metric(
                "drover_quality_status",
                1 if status == overall_status else 0,
                category="overall",
                status=status,
            )
        )
    for name, category in snapshot.get("categories", {}).items():
        current = category.get("status", "unknown")
        for status in statuses:
            lines.append(
                _metric(
                    "drover_quality_status",
                    1 if status == current else 0,
                    category=name,
                    status=status,
                )
            )

    audit = snapshot.get("runtime_audit", {})
    table_counts = audit.get("table_counts", {})
    lines.extend(
        [
            "# HELP drover_quality_table_rows Rows by Drover lakehouse relation.",
            "# TYPE drover_quality_table_rows gauge",
        ]
    )
    for table, count in sorted(table_counts.items()):
        if count is not None:
            lines.append(_metric("drover_quality_table_rows", count, table=table))

    bundle = snapshot["categories"].get("bundle_quality", {}).get("details", {})
    lines.extend(
        [
            "# HELP drover_quality_bundle_recall_usable_percent Percent of generated summaries usable for recall (summary text, completed status, embedding).",
            "# TYPE drover_quality_bundle_recall_usable_percent gauge",
            _metric(
                "drover_quality_bundle_recall_usable_percent",
                bundle.get("recall_usable_percent"),
            ),
            "# HELP drover_quality_bundle_rich_ready_percent Percent of generated summaries with stricter rich bundle metadata.",
            "# TYPE drover_quality_bundle_rich_ready_percent gauge",
            _metric(
                "drover_quality_bundle_rich_ready_percent",
                bundle.get("bundle_ready_percent"),
            ),
            "# HELP drover_quality_bundle_missing_summaries Generated summaries missing recall processing vs optional rich evidence.",
            "# TYPE drover_quality_bundle_missing_summaries gauge",
            _metric(
                "drover_quality_bundle_missing_summaries",
                bundle.get("missing_recall_processing_summaries"),
                kind="recall_processing",
            ),
            _metric(
                "drover_quality_bundle_missing_summaries",
                bundle.get("missing_rich_evidence_summaries"),
                kind="rich_evidence",
            ),
        ]
    )

    freshness = snapshot["categories"]["freshness"]["details"]
    lines.extend(
        [
            "# HELP drover_quality_freshness_latest_event_age_hours Latest event age by agent.",
            "# TYPE drover_quality_freshness_latest_event_age_hours gauge",
        ]
    )
    for agent_id, age in sorted(
        freshness.get("latest_event_age_hours_by_agent", {}).items()
    ):
        if age is not None:
            lines.append(
                _metric(
                    "drover_quality_freshness_latest_event_age_hours",
                    age,
                    agent_id=agent_id,
                )
            )
    lines.extend(
        [
            "# HELP drover_quality_freshness_latest_span_age_hours Latest span age.",
            "# TYPE drover_quality_freshness_latest_span_age_hours gauge",
            _metric(
                "drover_quality_freshness_latest_span_age_hours",
                freshness.get("latest_span_age_hours"),
            ),
            "# HELP drover_quality_unprocessed_incoming_files Unprocessed incoming JSONL files.",
            "# TYPE drover_quality_unprocessed_incoming_files gauge",
            _metric(
                "drover_quality_unprocessed_incoming_files",
                freshness.get("unprocessed_incoming_files", 0),
            ),
        ]
    )

    attribution = snapshot["categories"]["attribution"]["details"]
    lines.extend(
        [
            "# HELP drover_quality_repo_attribution_percent Repo attribution percent by agent.",
            "# TYPE drover_quality_repo_attribution_percent gauge",
        ]
    )
    for agent_id, percent in sorted(
        attribution.get("repo_attribution_percent_by_agent", {}).items()
    ):
        lines.append(
            _metric(
                "drover_quality_repo_attribution_percent",
                percent,
                agent_id=agent_id,
            )
        )

    identity = snapshot["categories"]["identity"]["details"]
    derived = snapshot["categories"]["derived_context"]["details"]
    lines.extend(
        [
            "# HELP drover_quality_identity_duplicate_id_values Duplicate agent_event id values.",
            "# TYPE drover_quality_identity_duplicate_id_values gauge",
            _metric(
                "drover_quality_identity_duplicate_id_values",
                identity.get("duplicate_id_values", 0),
            ),
            "# HELP drover_quality_identity_duplicate_dedup_key_values Duplicate canonical dedup_key values.",
            "# TYPE drover_quality_identity_duplicate_dedup_key_values gauge",
            _metric(
                "drover_quality_identity_duplicate_dedup_key_values",
                identity.get("duplicate_dedup_key_values", 0),
            ),
            "# HELP drover_quality_handoff_ready Whether derived context is ready for handoff.",
            "# TYPE drover_quality_handoff_ready gauge",
            _metric("drover_quality_handoff_ready", derived.get("handoff_ready", 0)),
            "# HELP drover_quality_warnings_total Total quality warnings.",
            "# TYPE drover_quality_warnings_total gauge",
            _metric("drover_quality_warnings_total", len(snapshot.get("warnings", []))),
        ]
    )
    return "\n".join(lines) + "\n"
