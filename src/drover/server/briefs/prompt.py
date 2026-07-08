"""Build the prompt sent to the backend for project-brief generation."""

from __future__ import annotations

from importlib.resources import files
from typing import Iterable, Optional

_SUMMARY_TRUNCATE = 1500  # per-summary cap to keep prompts bounded


def load_template() -> str:
    return (files("drover") / "prompts" / "project_brief.md").read_text()


def _format_summary(s: dict) -> str:
    sid = s.get("session_id", "?")
    ended = s.get("ended_at") or "—"
    summary = s.get("summary_md") or ""
    next_steps = s.get("next_steps_md") or ""
    files_touched = s.get("files_touched") or []
    open_q = s.get("open_questions") or []
    body = (
        f"summary: {summary}\n"
        f"next_steps: {next_steps}\n"
        f"files_touched: {', '.join(files_touched[:10])}\n"
        f"open_questions: {'; '.join(open_q[:5])}"
    )
    if len(body) > _SUMMARY_TRUNCATE:
        body = body[:_SUMMARY_TRUNCATE] + "...[truncated]"
    return f"[{ended}] session {sid}:\n{body}"


def build_brief_prompt(
    *,
    summaries: Iterable[dict],
    project_key: str,
    repo_owner: str,
    repo_name: str,
    session_count: int,
    last_activity_at: Optional[str],
    template: Optional[str] = None,
) -> str:
    summaries_list = list(summaries)
    body = (
        "\n\n---\n\n".join(_format_summary(s) for s in summaries_list)
        or "(no summaries yet)"
    )
    tmpl = template if template is not None else load_template()
    return tmpl.format(
        project_key=project_key,
        repo_owner=repo_owner,
        repo_name=repo_name,
        session_count=session_count,
        last_activity_at=last_activity_at or "—",
        summaries=body,
    )
