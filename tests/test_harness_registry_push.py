"""The awaiting-transition push hook on HarnessRegistry.

``update_session_activity`` is the single point both the local ``emit()`` path
and the remote ``/harness/events`` ingest path funnel through, so these tests
are what guarantee issue #20's "one transition, at most one notification".
"""

from __future__ import annotations

import json
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
    assert isinstance(transition.started_at, datetime)


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


def _structured_event(seq, event_type, payload):
    return {
        "event_id": f"structured-{seq}",
        "session_id": "sess-1",
        "event_type": event_type,
        "payload": payload,
        "seq": seq,
        "created_at": datetime(2026, 8, 12, 1, seq, tzinfo=timezone.utc),
    }


def test_structured_startup_ready_input_is_recorded_but_does_not_alert(
    registry, sender
):
    registry.ingest_structured_events(
        [_structured_event(1, "status", {"awaiting": "input"})]
    )

    assert registry.get_session("sess-1").awaiting == "input"
    assert sender.sent == []


def test_local_structured_startup_ready_input_is_recorded_but_does_not_alert(
    registry, sender
):
    registry.append_event(
        session_id="sess-1",
        event_type="status",
        payload={"awaiting": "input"},
        seq=1,
    )

    registry.update_session_activity(
        "sess-1", awaiting="input", structured_startup_input=True
    )

    assert registry.get_session("sess-1").awaiting == "input"
    assert sender.sent == []


def test_structured_startup_ready_is_silent_in_a_combined_batch_and_on_replay(
    registry, sender
):
    batch = [
        _structured_event(1, "status", {"awaiting": "input"}),
        _structured_event(2, "user_input", {"text": "Fix the notification"}),
    ]

    assert registry.ingest_structured_events(batch) == 2
    assert registry.get_session("sess-1").awaiting is None
    assert sender.sent == []
    assert registry.ingest_structured_events(batch) == 0
    assert sender.sent == []


def test_structured_post_task_question_alerts_after_silent_startup(registry, sender):
    registry.ingest_structured_events(
        [_structured_event(1, "status", {"awaiting": "input"})]
    )
    registry.ingest_structured_events(
        [
            _structured_event(2, "user_input", {"text": "Review this"}),
            _structured_event(3, "assistant_output", {"text": "Which option?"}),
            _structured_event(4, "status", {"awaiting": "input"}),
        ]
    )

    assert len(sender.sent) == 1
    assert sender.sent[-1].session_id == "sess-1"
    assert sender.sent[-1].needs_user
    assert sender.sent[-1].kind == "input"


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


def test_host_content_consent_off_never_exports_session_text(registry, sender):
    with registry._connect() as con:
        con.execute(
            "UPDATE harness_hosts SET capabilities_json = ? WHERE host_id = 'mac-mini'",
            [json.dumps({"content_consent": False})],
        )
    registry.append_event(
        session_id="sess-1",
        event_type="user_input",
        payload={"text": "**Secret migration**"},
    )
    _assistant_says(registry, "Deploy using private token", seq=1)
    registry.update_session_activity("sess-1", awaiting="input")

    transition = sender.sent[0]
    assert not transition.content_allowed
    assert transition.alert_title() == "drover needs your input"
    assert transition.alert_body().startswith("Claude Code session · opened ")
    assert "Secret" not in transition.alert_body()
    assert "token" not in transition.alert_body()


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
    assert sender.sent[0].alert_title() == "drover · Fix capture preview"
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
    assert sender.sent[0].alert_title() == "drover finished a session"
    assert sender.sent[0].alert_body() == "Agent session finished"


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
    assert sender.sent[0].alert_title() == "drover · Fix capture preview"


def test_alert_title_deduplicates_equal_project_and_prompt_labels(registry, sender):
    registry.append_event(
        session_id="sess-1",
        event_type="user_input",
        payload={"text": "drover"},
    )
    registry.update_session_activity("sess-1", awaiting="input")

    assert sender.sent[0].alert_title() == "drover"


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


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("is_error,kind", [(False, "finished"), (True, "failed")])
def test_snapshot_turn_kind_distinguishes_completion_from_question(
    registry, nested, is_error, kind
):
    inner = {
        "turn_complete": True,
        "awaiting": "input",
        "result": {"is_error": is_error},
    }
    registry.append_event(
        session_id="sess-1",
        event_type="status",
        seq=1,
        payload={"payload": inner} if nested else inner,
    )
    assert registry.completed_turn_kinds(["sess-1"]) == {"sess-1": kind}
    from drover.server.metrics import _harness_session_dict

    item = _harness_session_dict(registry.get_session("sess-1"), notification_kind=kind)
    assert item["notification_kind"] == kind
    assert item["status"] == "running"
    registry.append_event(
        session_id="sess-1", event_type="status", seq=2, payload={"awaiting": "input"}
    )
    assert registry.completed_turn_kinds(["sess-1"]) == {}
    assert registry.completed_turn_kinds([]) == {}


def test_completed_turn_kind_reads_postgres_payload_relation(pg_control_path):
    registry = HarnessRegistry(pg_control_path)
    registry.register_host(
        host_id="fixture", display_name="Fixture", kind="macos", capabilities={}
    )
    registry.create_session(
        session_id="fixture-session",
        host_id="fixture",
        harness="codex",
        command="codex",
        status="running",
    )
    registry.append_event(
        session_id="fixture-session",
        event_type="status",
        seq=1,
        payload={"payload": {"turn_complete": True, "awaiting": "input"}},
    )
    assert registry.completed_turn_kinds(["fixture-session"]) == {
        "fixture-session": "finished"
    }


def test_successful_completed_turn_requires_its_exact_latest_turn(registry):
    registry.append_event(
        session_id="sess-1", event_type="user_input", payload={"turn_id": "first"}
    )
    registry.append_event(
        session_id="sess-1",
        event_type="status",
        payload={"turn_id": "first", "payload": {"exited": 0}},
    )
    assert registry.has_successfully_completed_turn("sess-1", "first")

    # A newer active turn fences the earlier success instead of borrowing it.
    registry.append_event(
        session_id="sess-1", event_type="user_input", payload={"turn_id": "newer"}
    )
    assert not registry.has_successfully_completed_turn("sess-1", "first")
    assert not registry.has_successfully_completed_turn("sess-1", "newer")

    registry.append_event(
        session_id="sess-1",
        event_type="status",
        payload={"turn_id": "newer", "payload": {"exited": 1}},
    )
    assert not registry.has_successfully_completed_turn("sess-1", "newer")


def test_successful_completed_turn_rejects_unknown_terminal_status(registry):
    registry.append_event(
        session_id="sess-1", event_type="user_input", payload={"turn_id": "exact"}
    )
    registry.append_event(
        session_id="sess-1",
        event_type="status",
        payload={"turn_id": "exact", "payload": {"turn_complete": True}},
    )
    assert not registry.has_successfully_completed_turn("sess-1", "exact")


def test_completed_turn_kind_recognizes_turn_exited_status(registry):
    registry.append_event(
        session_id="sess-1",
        event_type="status",
        seq=1,
        payload={"payload": {"turn_complete": True, "awaiting": "input"}},
    )
    registry.append_event(
        session_id="sess-1",
        event_type="status",
        seq=2,
        payload={"payload": {"exited": 0}},
    )
    assert registry.completed_turn_kinds(["sess-1"]) == {"sess-1": "finished"}

    registry.append_event(
        session_id="sess-1",
        event_type="status",
        seq=3,
        payload={"payload": {"exited": 1}},
    )
    assert registry.completed_turn_kinds(["sess-1"]) == {"sess-1": "failed"}


def test_attention_generation_survives_missed_clear_and_heartbeat(registry):
    registry.update_session_activity("sess-1", awaiting="input")
    first = registry.get_session("sess-1").attention_id
    assert first
    registry.update_session_activity("sess-1", awaiting="input")
    assert registry.get_session("sess-1").attention_id == first
    registry.update_session_activity("sess-1", awaiting=None)
    assert registry.get_session("sess-1").attention_id is None
    registry.update_session_activity("sess-1", awaiting="input")
    second = registry.get_session("sess-1").attention_id
    assert second and second != first


def test_recovery_creates_fresh_attention_generation(registry):
    registry.update_session_activity("sess-1", awaiting="input")
    first = registry.get_session("sess-1").attention_id
    recovered = registry.mark_session_recovered("sess-1", "native-new")
    assert recovered.attention_id and recovered.attention_id != first


def test_structured_attention_batch_generation(registry):
    def event(seq, kind, payload):
        return {
            "event_id": f"attention-{seq}",
            "session_id": "sess-1",
            "event_type": kind,
            "payload": payload,
            "seq": seq,
            "created_at": datetime.now(timezone.utc),
        }

    first = event(1, "approval_prompt", {"prompt": "First?"})
    registry.ingest_structured_events([first])
    original = registry.get_session("sess-1").attention_id
    assert original == "attention-1"
    batch = [
        event(2, "user_input", {}),
        event(3, "approval_prompt", {"prompt": "Second?"}),
    ]
    registry.ingest_structured_events(batch)
    second = registry.get_session("sess-1").attention_id
    assert second == "attention-3" and second != original
    registry.ingest_structured_events(batch)
    assert registry.get_session("sess-1").attention_id == second
    registry.ingest_structured_events([event(4, "session.exited", {"exited": 0})])
    assert registry.get_session("sess-1").attention_id is None


def test_terminal_status_clears_attention_generation(registry):
    registry.update_session_activity("sess-1", awaiting="input")
    assert registry.get_session("sess-1").attention_id
    registry.update_session_status("sess-1", "completed")
    assert registry.get_session("sess-1").attention_id is None
