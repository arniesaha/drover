"""Tests for drover.hook.render — handoff payload formatting."""

from __future__ import annotations

from drover.hook.render import render_handoff


def test_render_with_summary_and_active_peer() -> None:
    payload = {
        "task_id": "abc1234567890def",
        "repo_owner": "arniesaha",
        "repo_name": "nexus",
        "branch": "feat/foo",
        "summaries": [
            {
                "session_id": "s-old",
                "agent_id": "nas-claude",
                "ended_at": "2026-05-09T00:00:00+00:00",
                "summary_md": "Wired Plan 5 summarizer end-to-end.",
                "next_steps_md": "Build Plan 6 nexus-hook.",
                "open_questions": ["should claude-haiku-4-5-20251001 be the default?"],
            }
        ],
        "active_sessions": [
            {
                "session_id": "s-active",
                "agent_id": "macmini-claude",
                "started_at": "2026-05-09T01:00:00+00:00",
                "last_event_at": "2026-05-09T01:05:00+00:00",
            }
        ],
    }
    out = render_handoff(payload)
    assert "Resuming task" in out
    assert "abc1234" in out
    assert "feat/foo" in out
    assert "Wired Plan 5" in out
    assert "Build Plan 6" in out
    assert "macmini-claude" in out  # active peer warning


def test_render_no_summary_no_peer() -> None:
    payload = {
        "task_id": "deadbeef00000000",
        "repo_owner": "x",
        "repo_name": "y",
        "branch": "z",
        "summaries": [],
        "active_sessions": [],
    }
    out = render_handoff(payload)
    assert "no prior summaries" in out.lower() or "no recent" in out.lower()
    assert "deadbeef" in out


def test_render_handles_missing_branch() -> None:
    payload = {
        "task_id": "tid",
        "repo_owner": "x",
        "repo_name": "y",
        "branch": None,
        "summaries": [],
        "active_sessions": [],
    }
    out = render_handoff(payload)
    assert "tid" in out


def test_render_truncates_long_summary() -> None:
    long = "x" * 5000
    payload = {
        "task_id": "tid",
        "repo_owner": "r",
        "repo_name": "n",
        "branch": "b",
        "summaries": [
            {
                "session_id": "s",
                "agent_id": "a",
                "ended_at": "2026-05-09T00:00Z",
                "summary_md": long,
                "next_steps_md": "ok",
                "open_questions": [],
            }
        ],
        "active_sessions": [],
    }
    out = render_handoff(payload)
    assert long not in out  # something shorter rendered
