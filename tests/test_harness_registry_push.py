"""The awaiting-transition push hook on HarnessRegistry.

``update_session_activity`` is the single point both the local ``emit()`` path
and the remote ``/harness/events`` ingest path funnel through, so these tests
are what guarantee issue #20's "one transition, at most one notification".
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from drover.schema import bootstrap
from drover.server.harness.registry import HarnessRegistry
from drover.server.push import AwaitingTransition, set_sender


class RecordingSender:
    def __init__(self):
        self.sent: list[AwaitingTransition] = []

    def notify(self, transition):
        self.sent.append(transition)


@pytest.fixture
def sender():
    recorder = RecordingSender()
    set_sender(recorder)
    try:
        yield recorder
    finally:
        set_sender(None)


@pytest.fixture
def registry(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    reg = HarnessRegistry(duckdb_path)
    reg.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        capabilities={"harnesses": ["claude-code"]},
    )
    reg.create_session(
        session_id="sess-1",
        host_id="mac-mini",
        harness="claude-code",
        command="claude",
        cwd="/Users/x/work/drover",
        status="running",
        started_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    return reg


def test_entering_awaiting_notifies_once(registry, sender):
    registry.update_session_activity("sess-1", awaiting="approval")

    assert len(sender.sent) == 1
    transition = sender.sent[0]
    assert transition.session_id == "sess-1"
    assert transition.awaiting == "approval"
    assert transition.harness == "claude-code"
    assert transition.cwd == "/Users/x/work/drover"


def test_repeated_awaiting_does_not_re_notify(registry, sender):
    # A harness that re-emits "still awaiting input" every few seconds must
    # not produce a banner every few seconds.
    for _ in range(5):
        registry.update_session_activity("sess-1", awaiting="input")

    assert len(sender.sent) == 1


def test_resolving_and_re_entering_notifies_again(registry, sender):
    registry.update_session_activity("sess-1", awaiting="input")
    registry.update_session_activity("sess-1", awaiting=None)
    registry.update_session_activity("sess-1", awaiting="input")

    # Two real "needs you" moments, plus the clear in between.
    assert [t.awaiting for t in sender.sent] == ["input", None, "input"]


def test_changing_between_awaiting_kinds_notifies(registry, sender):
    registry.update_session_activity("sess-1", awaiting="input")
    registry.update_session_activity("sess-1", awaiting="approval")

    assert [t.awaiting for t in sender.sent] == ["input", "approval"]


def test_leaving_awaiting_is_dispatched_but_not_alertable(registry, sender):
    registry.update_session_activity("sess-1", awaiting="input")
    registry.update_session_activity("sess-1", awaiting=None)

    # The sender is what decides silence, so the badge can still be refreshed
    # on a clear without the user being alerted about it.
    assert sender.sent[-1].needs_user is False


def test_unknown_session_never_notifies(registry, sender):
    registry.update_session_activity("no-such-session", awaiting="input")

    assert sender.sent == []


def test_activity_recording_survives_a_broken_sender(registry):
    class Broken:
        def notify(self, transition):
            raise RuntimeError("boom")

    set_sender(Broken())
    try:
        registry.update_session_activity("sess-1", awaiting="input")
    finally:
        set_sender(None)

    # The write is the product; push is a courtesy on top of it.
    assert registry.get_session("sess-1").awaiting == "input"


def test_activity_is_still_recorded_when_no_sender_is_registered(registry):
    set_sender(None)
    registry.update_session_activity("sess-1", awaiting="approval")

    assert registry.get_session("sess-1").awaiting == "approval"


# --- notification preview ---------------------------------------------------


def _assistant_says(registry, text, *, seq, session_id="sess-1"):
    registry.append_event(
        session_id=session_id,
        event_type="assistant_output",
        content_preview=text,
        seq=seq,
    )


def test_alert_carries_what_the_agent_last_said(registry, sender):
    _assistant_says(registry, "Ready to deploy. Want me to push?", seq=1)

    registry.update_session_activity("sess-1", awaiting="input")

    assert sender.sent[0].preview == "Ready to deploy. Want me to push?"


def test_newest_message_wins(registry, sender):
    _assistant_says(registry, "an older thought", seq=1)
    _assistant_says(registry, "the latest word", seq=2)

    registry.update_session_activity("sess-1", awaiting="input")

    assert sender.sent[0].preview == "the latest word"


def test_thinking_only_turns_are_skipped(registry, sender):
    # The harness stores the event type as the preview when a turn produced
    # no visible text; showing "assistant_output" as the body would be worse
    # than showing nothing.
    _assistant_says(registry, "the real message", seq=1)
    _assistant_says(registry, "assistant_output", seq=2)

    registry.update_session_activity("sess-1", awaiting="input")

    assert sender.sent[0].preview == "the real message"


def test_session_with_nothing_said_yet_has_no_preview(registry, sender):
    registry.update_session_activity("sess-1", awaiting="input")

    assert sender.sent[0].preview == ""
    # And still alerts, on the old generic wording.
    assert sender.sent[0].alert_body() == "Needs your input: reply in the app"


def test_preview_is_redacted_before_it_leaves_the_host(registry, sender):
    # This text goes to Apple's servers, so the same redaction the session
    # list uses has to apply here too.
    _assistant_says(registry, "run: export ANTHROPIC_API_KEY=sk-ant-secret123", seq=1)

    registry.update_session_activity("sess-1", awaiting="input")

    assert "sk-ant-secret123" not in sender.sent[0].preview


def test_other_sessions_messages_are_not_borrowed(registry, sender):
    registry.create_session(
        session_id="sess-2",
        host_id="mac-mini",
        harness="codex",
        command="codex",
        cwd="/Users/x/work/other",
        status="running",
        started_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    _assistant_says(registry, "belongs to sess-2", seq=1, session_id="sess-2")

    registry.update_session_activity("sess-1", awaiting="input")

    assert sender.sent[0].preview == ""


def test_clearing_awaiting_does_not_pay_for_a_preview(registry, sender):
    _assistant_says(registry, "some message", seq=1)
    registry.update_session_activity("sess-1", awaiting="input")
    registry.update_session_activity("sess-1", awaiting=None)

    # The clear still dispatches (the badge cares), but skips the query for
    # text no notification will ever show.
    assert sender.sent[-1].awaiting is None
    assert sender.sent[-1].preview == ""


def test_archived_session_ingest_does_not_resurrect_awaiting_or_fire_push(
    registry, sender
):
    registry.update_session_status("sess-1", "completed")
    sender.sent.clear()

    # Ingest late events that would otherwise trigger an awaiting transition
    registry.ingest_structured_events(
        [
            {
                "event_id": "evt-prompt",
                "session_id": "sess-1",
                "event_type": "approval_prompt",
                "payload": {"prompt": "Allow rm -rf?"},
                "created_at": datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc),
                "seq": 1,
            }
        ]
    )

    session = registry.get_session("sess-1")
    assert session.status == "completed"
    assert session.awaiting is None
    assert len(sender.sent) == 0


def test_update_session_activity_on_archived_session_does_not_set_awaiting(
    registry, sender
):
    registry.update_session_status("sess-1", "terminated")
    sender.sent.clear()

    registry.update_session_activity("sess-1", awaiting="input")

    session = registry.get_session("sess-1")
    assert session.status == "terminated"
    assert session.awaiting is None
    assert len(sender.sent) == 0


def test_derive_structured_awaiting_clears_on_session_exited(registry, sender):
    from drover.server.harness.registry import _derive_structured_awaiting

    assert (
        _derive_structured_awaiting(
            event_type="approval_prompt", payload={}, current=None
        )
        == "approval"
    )
    assert (
        _derive_structured_awaiting(
            event_type="session.exited", payload={}, current="approval"
        )
        is None
    )


def test_terminal_status_notifies_once_and_uses_assistant_summary(registry, sender):
    _assistant_says(registry, "Capture is ready for review", seq=1)
    registry.update_session_status("sess-1", "completed")
    registry.update_session_status("sess-1", "completed")
    assert len(sender.sent) == 1
    assert sender.sent[0].kind == "finished"
    assert sender.sent[0].alert_body() == "Finished: Capture is ready for review"


def test_other_sessions_private_profiles_do_not_hide_title_or_summary(registry, sender):
    with registry._connect() as con:
        con.execute("CREATE TABLE profile_items (tier TEXT)")
        con.execute("INSERT INTO profile_items VALUES ('private')")
        con.execute("CREATE TABLE profile_agents (tier TEXT)")
        con.execute("INSERT INTO profile_agents VALUES ('private')")
        con.execute("CREATE TABLE profile_proposals (session_id TEXT)")
        con.execute("INSERT INTO profile_proposals VALUES ('other-session')")
    registry.create_session(
        session_id="other-session",
        host_id="mac-mini",
        harness="claude-code",
        command="claude",
    )
    registry.append_event(
        session_id="other-session",
        event_type="tool_action",
        payload={"tool": "drover_profile"},
    )
    registry.append_event(
        session_id="sess-1",
        event_type="user_input",
        payload={"text": "Fix capture preview"},
    )
    _assistant_says(registry, "Capture is ready for review", seq=1)
    registry.update_session_status("sess-1", "completed")
    assert not sender.sent[0].private
    assert sender.sent[0].alert_title() == "Fix capture preview"
    assert sender.sent[0].alert_body() == "Finished: Capture is ready for review"


@pytest.mark.parametrize(
    "event_type,payload",
    [
        ("tool_action", {"tool": "drover_profile"}),
        (
            "tool_action",
            {
                "payload": {
                    "tool": "mcp__drover__drover_profile",
                    "input": {"scope": "first_turn"},
                }
            },
        ),
        ("tool_action", {"tool_name": "profile_load"}),
        ("tool_result", {"tool": "drover_profile"}),
        (
            "status",
            {
                "payload": {
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "drover",
                        "tool": "drover_profile",
                    }
                }
            },
        ),
        ("tool_action", {"tool": "shell", "input": {"command": "drover profile load"}}),
    ],
)
@pytest.mark.parametrize(
    "repo_name,want_title", [(None, "drover"), ("capture", "capture")]
)
def test_session_profile_read_preserves_public_title_and_drops_summary(
    registry, sender, event_type, payload, repo_name, want_title
):
    with registry._connect() as con:
        con.execute(
            "UPDATE harness_sessions SET repo_name = ? WHERE session_id = 'sess-1'",
            [repo_name],
        )
    registry.append_event(
        session_id="sess-1",
        event_type="user_input",
        payload={"text": "Synthetic private prompt"},
    )
    registry.append_event(session_id="sess-1", event_type=event_type, payload=payload)
    _assistant_says(registry, "Synthetic private context", seq=1)
    registry.update_session_status("sess-1", "completed")
    assert sender.sent[0].private
    assert sender.sent[0].preview == ""
    assert sender.sent[0].alert_title() == want_title
    assert sender.sent[0].alert_body() == "Finished: ready for review"


def test_profile_mention_in_assistant_text_is_not_a_profile_read(registry, sender):
    registry.append_event(
        session_id="sess-1",
        event_type="tool_action",
        payload={"tool": "read_file", "input": {"file": "profile.py"}},
    )
    _assistant_says(registry, "Add tests for drover_profile", seq=1)
    registry.update_session_activity("sess-1", awaiting="input")
    assert not sender.sent[0].private
    assert "Add tests" in sender.sent[0].alert_body()


def test_notification_privacy_exception_fails_closed(registry, sender, monkeypatch):
    def broken_connection():
        raise RuntimeError("Synthetic unavailable event store")

    monkeypatch.setattr(registry, "_connect", broken_connection)
    registry._dispatch_session_push(
        session_id="sess-1",
        status="completed",
        cwd="capture",
        preview="Synthetic private text",
    )
    assert sender.sent[0].private
    assert sender.sent[0].alert_title() == "Session"
    assert sender.sent[0].alert_body() == "Finished: ready for review"


@pytest.mark.parametrize("exit_code,kind", [(0, "finished"), (1, "failed")])
def test_remote_terminal_event_notifies_once_on_replay(
    registry, sender, exit_code, kind
):
    from datetime import datetime, timezone

    record = {
        "event_id": "synthetic-exit",
        "session_id": "sess-1",
        "event_type": "session.exited",
        "payload": {"payload": {"exited": exit_code}},
        "seq": 1,
        "created_at": datetime.now(timezone.utc),
    }
    registry.ingest_structured_events([record])
    registry.ingest_structured_events([record])
    assert len(sender.sent) == 1
    assert sender.sent[0].kind == kind


def test_title_is_derived_from_first_prompt(registry, sender):
    registry.append_event(
        session_id="sess-1",
        event_type="user_input",
        payload={"text": "Fix capture preview"},
    )
    registry.update_session_activity("sess-1", awaiting="input")
    assert sender.sent[0].alert_title() == "Fix capture preview"


@pytest.mark.parametrize("is_error,kind", [(False, "finished"), (True, "failed")])
def test_completed_turn_is_normal_even_though_session_accepts_input(
    registry, sender, is_error, kind
):
    _assistant_says(registry, "Capture ready for review", seq=1)
    registry.append_event(
        session_id="sess-1",
        event_type="status",
        seq=2,
        payload={
            "payload": {
                "turn_complete": True,
                "awaiting": "input",
                "result": {"is_error": is_error},
            }
        },
    )
    registry.update_session_activity("sess-1", awaiting="input")
    assert sender.sent[0].kind == kind
    assert not sender.sent[0].needs_user
    assert registry.get_session("sess-1").status == "running"
