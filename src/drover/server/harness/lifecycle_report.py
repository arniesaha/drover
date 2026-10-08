"""Report-only policy: decisions are data, never commands."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from drover.server.harness.registry import _db_timestamp_to_utc


def session_decision(session, publications, owners, now, idle_seconds):
    reasons = []
    if session.status != "running" or session.ended_at is not None:
        reasons.append("not_confirmed_running")
    if session.effective_awaiting != "input" or session.mode != "structured":
        reasons.append("not_structured_input_wait")
    activity = _db_timestamp_to_utc(session.last_activity)
    if activity is None or activity > now:
        reasons.append("unknown_or_future_activity")
    elif now - activity < timedelta(seconds=idle_seconds):
        reasons.append("within_idle_window")
    if session.retention_policy == "keep":
        reasons.append("explicit_keep")
    elif session.retention_policy not in ("auto", "archive"):
        reasons.append("unknown_retention_policy")
    if session.harness in {"collector", "drover-collect", "otel-collector"}:
        reasons.append("collector")
    if (
        session.source_session_id
        and session.source_session_id.startswith("factory/")
        and owners is not None
        and not any(o["session_id"] == session.session_id for o in owners)
    ):
        reasons.append("factory_owner_evidence_missing")
    if owners is None:
        reasons.append("owner_lookup_unknown")
    else:
        for owner in owners:
            if (
                owner["session_id"] == session.session_id
                and owner["owner_id"]
                and owner["lease_until"]
                and _db_timestamp_to_utc(owner["lease_until"]) > now
            ):
                reasons.append("live_factory_owner")
                break
    # No configured PR lookup exists yet. Missing and stale evidence protect.
    if not publications:
        reasons.append("publication_evidence_missing")
    for p in publications:
        verified = _db_timestamp_to_utc(p["pr_verified_at"])
        if p["pr_state"] == "open":
            reasons.append("open_pr")
        elif (
            verified is None or verified > now or now - verified > timedelta(minutes=15)
        ):
            reasons.append("pr_verification_unknown_or_stale")
        elif p["pr_state"] not in ("merged", "closed"):
            reasons.append("pr_state_unknown")
    return {
        "session_id": session.session_id,
        "would_expire": not reasons,
        "reasons": sorted(set(reasons)) or ["input_idle_threshold_reached"],
    }


def report(collector, config, now=None):
    from drover.server.harness.lifecycle import LifecycleStore

    now = now or datetime.now(timezone.utc)
    result = {
        "mode": config.mode,
        "scanned_at": now.isoformat(),
        "sessions": [],
        "hosts": [],
    }
    if config.mode == "off":
        result["summary"] = {"would_expire": 0, "worktrees": 0, "unsupported_hosts": 0}
        return result
    store = LifecycleStore(collector.duckdb_path)
    registry = store.registry
    try:
        with registry._connect() as con:
            rows = con.execute(
                "SELECT session_id, owner_id, lease_until FROM factory_observer_runs"
            ).fetchall()
        owners = [dict(zip(("session_id", "owner_id", "lease_until"), r)) for r in rows]
    except Exception:
        owners = None
    hosts = registry.list_hosts()
    by_host = {h.host_id: h for h in hosts}
    for session in registry.list_sessions():
        result["sessions"].append(
            session_decision(
                session,
                store.publications(session.session_id),
                owners,
                now,
                config.idle_after_seconds,
            )
        )
        host = by_host.get(session.host_id)
        if host is None or not host.liveness().usable:
            result["sessions"][-1]["would_expire"] = False
            result["sessions"][-1]["reasons"].append("host_liveness_unverified")
    for host in hosts:
        entry = {"host_id": host.host_id, "state": "unsupported", "worktrees": []}
        if host.capabilities.get("lifecycle", {}).get("worktree_inventory") == 1:
            if not host.liveness().usable:
                entry["state"] = "unavailable"
            else:
                try:
                    import json

                    status, body = collector._harness_request(
                        host,
                        "/lifecycle/worktrees",
                        method="GET",
                        payload={},
                        timeout_s=10.0,
                    )
                    payload = json.loads(body)
                    if status != 200 or not isinstance(payload.get("worktrees"), list):
                        raise ValueError("invalid inventory")
                    entry.update(
                        state="reported",
                        worktrees=payload["worktrees"],
                        errors=payload.get("errors", []),
                    )
                    for tree in entry["worktrees"]:
                        sid = tree.get("session_id")
                        session = registry.get_session(sid) if sid else None
                        if session is None or session.host_id != host.host_id:
                            tree.update(
                                session_id=None,
                                ownership="foreign",
                                would_collect=False,
                                reasons=["foreign_or_ambiguous_ownership"],
                            )
                        if session and (
                            session.retention_policy == "keep"
                            or session.status
                            not in ("completed", "terminated", "errored", "failed")
                        ):
                            tree["would_collect"] = False
                            tree["reasons"].append("explicit_keep_or_live_session")
                        # PR/ownership lookup is not complete, so collection remains retained.
                        if tree.get("would_collect"):
                            tree["would_collect"] = False
                            tree["reasons"].append(
                                "requires_verified_publication_and_owner_checks"
                            )
                    store.record_inventory(host.host_id, entry["worktrees"], now)
                except Exception:
                    entry.update(state="unavailable", worktrees=[])
        result["hosts"].append(entry)
    result["summary"] = {
        "would_expire": sum(s["would_expire"] for s in result["sessions"]),
        "worktrees": sum(len(h["worktrees"]) for h in result["hosts"]),
        "unsupported_hosts": sum(h["state"] == "unsupported" for h in result["hosts"]),
    }
    return result


def start_reporter(collector, stop):
    """Periodic observations only; duplicate hub reports are harmless."""
    import logging
    import random
    import threading

    if collector.lifecycle_config.mode == "off":
        return None

    def run():
        while not stop.is_set():
            try:
                collector.lifecycle_report()
            except Exception:
                logging.getLogger(__name__).warning(
                    "lifecycle scan unavailable", exc_info=False
                )
            if stop.wait(300 + random.uniform(0, 30)):
                break

    thread = threading.Thread(target=run, name="lifecycle-report", daemon=True)
    thread.start()
    return thread
