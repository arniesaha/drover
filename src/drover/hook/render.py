"""Format the handoff payload as a markdown block the agent can read.

Intentionally compact (~1500-token target per spec §6.1 step 4): the
hook prepends this to the agent's system context, so every line costs.
"""

from __future__ import annotations

from typing import Any, Optional

_SUMMARY_TRUNCATE = 800
_NEXT_STEPS_TRUNCATE = 400


def _short_id(s: str, n: int = 8) -> str:
    return (s or "")[:n]


def _truncate(s: Optional[str], n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _format_active_peers(
    active: list[dict], own_session_id: Optional[str] = None
) -> str:
    peers = [s for s in active if s.get("session_id") != own_session_id]
    if not peers:
        return ""
    lines = []
    for p in peers[:5]:
        lines.append(
            f"- `{p.get('agent_id', '?')}` (session `{_short_id(p.get('session_id', ''))}…`, "
            f"last event {p.get('last_event_at', '?')})"
        )
    return "\n⚠️  Other agents currently active on this task:\n" + "\n".join(lines)


def render_handoff(payload: dict, *, own_session_id: Optional[str] = None) -> str:
    task_id = payload.get("task_id") or "(unknown)"
    repo_owner = payload.get("repo_owner") or "?"
    repo_name = payload.get("repo_name") or "?"
    branch = payload.get("branch")
    summaries = payload.get("summaries") or []
    active = payload.get("active_sessions") or []

    branch_label = f"@{branch}" if branch else ""
    head = f"**Resuming task `{repo_owner}/{repo_name}{branch_label}`** (task_id: `{_short_id(task_id, 8)}…`)"

    if not summaries:
        body = "_No prior summaries for this task — first session._"
    else:
        s = summaries[0]
        body = "\n".join(
            [
                f"_Last touched by `{s.get('agent_id', '?')}` (session `{_short_id(s.get('session_id', ''))}…`, "
                f"ended {s.get('ended_at', '?')})._",
                "",
                "**Summary:** " + _truncate(s.get("summary_md"), _SUMMARY_TRUNCATE),
            ]
        )
        if s.get("next_steps_md"):
            body += "\n\n**Next steps:** " + _truncate(
                s.get("next_steps_md"), _NEXT_STEPS_TRUNCATE
            )
        oq = s.get("open_questions") or []
        if oq:
            body += "\n\n**Open questions:**\n" + "\n".join(f"- {q}" for q in oq[:5])

    return head + "\n\n" + body + _format_active_peers(active, own_session_id)
