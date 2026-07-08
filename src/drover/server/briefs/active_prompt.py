"""Build the prompt sent to the backend for active-session-brief generation.

Mirrors ``drover.server.briefs.prompt`` but targets the rolling handoff
schema used for OPEN sessions (see ``ACTIVE_BRIEF_REQUIRED_KEYS``).
"""

from __future__ import annotations

from importlib.resources import files
from typing import Iterable, Optional

_TURN_TRUNCATE = 300  # per-event char cap — tight, this prompt runs hot
_MAX_EVENTS = 30


def load_template() -> str:
    return (files("drover") / "prompts" / "active_session_brief.md").read_text()


def _format_turn(ev: dict) -> str:
    role = ev.get("role") or ev.get("event_type", "unknown")
    ts = ev.get("timestamp", "")
    content = ev.get("content") or ""
    if len(content) > _TURN_TRUNCATE:
        content = content[:_TURN_TRUNCATE] + "...[truncated]"
    return f"[{ts}] {role}:\n{content}"


def build_active_brief_prompt(
    *,
    events: Iterable[dict],
    session_id: str,
    agent_id: str,
    started_at: Optional[str],
    ended_at: Optional[str],
    template: Optional[str] = None,
) -> str:
    events_list = list(events)[-_MAX_EVENTS:]
    n = len(events_list)
    turns = "\n\n---\n\n".join(_format_turn(e) for e in events_list) or "(no turns)"
    tmpl = template if template is not None else load_template()
    return tmpl.format(
        session_id=session_id,
        agent_id=agent_id,
        started_at=started_at or "—",
        ended_at=ended_at or "—",
        event_count=n,
        turns=turns,
    )
