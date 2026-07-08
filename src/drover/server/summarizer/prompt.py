"""Build the prompt sent to Claude for session summarization.

The template lives at ``src/drover/prompts/session_summary.md`` and is
versioned in git so prompt changes show up in PR diffs.
"""

from __future__ import annotations

from importlib.resources import files
from typing import Iterable, Optional

_TURN_TRUNCATE = 1500  # per-message char cap to keep prompts bounded


def load_template() -> str:
    return (files("drover") / "prompts" / "session_summary.md").read_text()


def _format_turn(ev: dict) -> str:
    role = ev.get("role") or ev.get("event_type", "unknown")
    ts = ev.get("timestamp", "")
    content = ev.get("content") or ""
    if len(content) > _TURN_TRUNCATE:
        content = content[:_TURN_TRUNCATE] + "...[truncated]"
    return f"[{ts}] {role}:\n{content}"


def build_summary_prompt(
    *,
    events: Iterable[dict],
    session_id: str,
    agent_id: str,
    started_at: Optional[str],
    ended_at: Optional[str],
    template: Optional[str] = None,
) -> str:
    events_list = list(events)
    n = len(events_list)
    turns = "\n\n---\n\n".join(_format_turn(e) for e in events_list) or "(no turns)"
    tmpl = template if template is not None else load_template()
    return tmpl.format(
        session_id=session_id,
        agent_id=agent_id,
        started_at=started_at or "—",
        ended_at=ended_at or "—",
        event_count=n,
        n_turns=f"{n} turns",
        turns=turns,
    )
