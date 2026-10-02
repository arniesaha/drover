"""Read-only session evidence checks shared by local and live MCP audits."""

from __future__ import annotations

from drover.event_identity import canonical_agent_events_cte
from drover.server.memory_identity import resolve_session
from drover.server.summarizer.derive import final_references


def audit_session(con, harness_id: str, *, store_path=None) -> dict:
    resolution = resolve_session(con, harness_id, store_path=store_path)
    out = {
        "harness_id": harness_id,
        "status": resolution["status"],
        "canonical_event_count": 0,
        "mapping_present": False,
        "summary_status": "unknown",
        "final_references": [],
        "summary_contains_final_references": False,
        "files_touched_non_empty": False,
    }
    if resolution["status"] != "ok" and not resolution.get("harness_session_id"):
        return out
    sid = resolution["session_id"]
    out["mapping_present"] = bool(resolution.get("harness_session_id"))
    out["native_mapping_present"] = bool(resolution.get("native_session_id"))
    out["summary_mapping_present"] = bool(resolution.get("summary_session_id"))
    source = (
        "control_memory_events"
        if resolution.get("harness_session_id")
        else "agent_events"
    )
    ctes = f"session_agent_events AS (SELECT * FROM {source} WHERE session_id=?), {canonical_agent_events_cte(source='session_agent_events')}"
    out["canonical_event_count"] = con.execute(
        f"WITH {ctes} SELECT count(*) FROM canonical_agent_events",
        [sid],
    ).fetchone()[0]
    final = con.execute(
        f"""WITH {ctes}
        SELECT content FROM canonical_agent_events WHERE role='assistant'
        AND event_type NOT IN ('tool_call', 'tool_action', 'tool_result')
        AND trim(coalesce(content,'')) <> '' ORDER BY timestamp DESC, id DESC LIMIT 1""",
        [sid],
    ).fetchone()
    refs = final_references(final[0] if final else "")
    out["final_references"] = refs
    from drover.server.ledger import (
        SUMMARIZE_SESSION,
        JobLedger,
        memory_store_available,
    )
    from drover.server.memory_store import MemoryRepository

    if store_path is None or not memory_store_available(store_path):
        out["summary_status"] = "unavailable"
        return out
    summary = MemoryRepository(store_path).summary(
        resolution.get("summary_session_id") or sid
    )
    if summary:
        out["summary_status"] = summary.status
        out["summary_contains_final_references"] = all(
            ref in summary.summary_md for ref in refs
        )
        out["files_touched_non_empty"] = bool(summary.files_touched)
    else:
        job = JobLedger(store_path).latest(SUMMARIZE_SESSION, sid)
        out["summary_status"] = (
            "insufficient_input"
            if job and job.error_category == "no_events"
            else job.status if job else "unavailable"
        )
    return out
