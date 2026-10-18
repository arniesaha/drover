"""Native history discovered exclusively from files under the launched HOME."""

from __future__ import annotations

import json

import pytest

from .test_harnessd_contract import request

CLAUDE_CWD = "/work/claude-project"
CODEX_CWD = "/work/codex-project"
CODEX_ID = "019ef2b6-7000-79c3-93c6-039d129b9513"


@pytest.mark.parametrize(
    "daemon",
    [
        {
            "home_files": {
                ".claude/projects/-work-claude-project/claude-session-1.jsonl": "\n".join(
                    [
                        json.dumps(
                            {"type": "last-prompt", "sessionId": "claude-session-1"}
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "cwd": CLAUDE_CWD,
                                "timestamp": "2026-06-23T01:00:00Z",
                            }
                        ),
                    ]
                )
            }
        }
    ],
    indirect=True,
)
def test_discovers_claude_native_resume_sessions(daemon):
    status, body = request(
        daemon, f"/native-sessions?harness=claude-code&cwd={CLAUDE_CWD}"
    )
    assert status == 200
    session = body["sessions"][0]
    assert (session["harness"], session["session_id"], session["cwd"]) == (
        "claude-code",
        "claude-session-1",
        CLAUDE_CWD,
    )
    assert session["native_resume"]["session_id"] == "claude-session-1"


@pytest.mark.parametrize(
    "daemon",
    [
        {
            "home_files": {
                f".codex/sessions/2026/06/23/rollout-2026-06-23T01-00-00-{CODEX_ID}.jsonl": "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {"id": CODEX_ID, "cwd": CODEX_CWD},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"cwd": CODEX_CWD, "type": "task_started"},
                            }
                        ),
                    ]
                )
            }
        }
    ],
    indirect=True,
)
def test_discovers_codex_native_resume_sessions(daemon):
    status, body = request(daemon, f"/native-sessions?harness=codex&cwd={CODEX_CWD}")
    assert status == 200
    session = body["sessions"][0]
    assert (session["harness"], session["session_id"], session["cwd"]) == (
        "codex",
        CODEX_ID,
        CODEX_CWD,
    )
    assert session["native_resume"]["session_id"] == CODEX_ID


@pytest.mark.parametrize("daemon", [{"home_files": {}}], indirect=True)
def test_native_sessions_query_is_empty_without_history(daemon):
    assert request(daemon, "/native-sessions?harness=codex")[1]["sessions"] == []


def test_native_session_inventory_enforces_its_public_hard_cap(daemon):
    session_dir = daemon.root / "home" / ".claude" / "projects" / "project"
    session_dir.mkdir(parents=True)
    for index in range(101):
        session_id = f"session-{index:03d}"
        (session_dir / f"{session_id}.jsonl").write_text(
            json.dumps({"type": "last-prompt", "sessionId": session_id})
            + "\n"
            + json.dumps({"type": "user", "cwd": "/work/project"})
        )
    status, body = request(
        daemon, "/native-sessions?harness=claude-code&cwd=/work/project&limit=1000"
    )
    assert status == 200
    assert len(body["sessions"]) == 100
