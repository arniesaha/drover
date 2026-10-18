"""Public inventory pagination after real session lifecycle transitions."""

from __future__ import annotations

from .test_harnessd_contract import request


def _terminated_structured(daemon):
    status, created = request(
        daemon,
        "/sessions",
        payload={
            "harness": "claude-code",
            "mode": "structured",
            "cwd": str(daemon.root),
        },
    )
    assert status == 201
    status, body = request(
        daemon, f"/sessions/{created['session_id']}", method="DELETE"
    )
    assert (status, body["status"]) == (200, "terminated")
    return created["session_id"]


def test_harnessd_session_listing_bounds_history_but_keeps_live_sessions_first(daemon):
    archived = [_terminated_structured(daemon) for _ in range(3)]
    status, page = request(daemon, "/sessions?archived=2")
    assert status == 200
    assert page["archived_limit"] == 2
    assert [item["session_id"] for item in page["sessions"]] == archived[::-1][:2]
    assert page["next_archived_cursor"]


def test_harnessd_session_listing_cursor_pages_through_history_once(daemon):
    archived = [_terminated_structured(daemon) for _ in range(3)]
    status, first = request(daemon, "/sessions?archived=2")
    assert status == 200
    cursor = first["next_archived_cursor"]
    status, second = request(daemon, f"/sessions?archived=2&archived_cursor={cursor}")
    assert status == 200
    seen = [item["session_id"] for item in first["sessions"] + second["sessions"]]
    assert seen == archived[::-1]
    assert second["next_archived_cursor"] is None


def test_harnessd_reconciles_exited_sessions_out_of_live_inventory(daemon):
    _terminated_structured(daemon)
    status, page = request(daemon, "/sessions?archived=0")
    assert status == 200
    assert page["sessions"] == []
