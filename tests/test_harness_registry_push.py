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
