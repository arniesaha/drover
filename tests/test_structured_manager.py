"""Unit tests for StructuredSessionManager's correctness-critical invariants.

These use a lightweight stub driver (no real subprocess) so they run fast and
exercise exactly the manager logic that is hard to pin down through slow,
timing-sensitive subprocess E2E tests:

- Finalize is gated on `turn_id is None`: Codex/Gemini's per-turn respawn
  drivers emit a `status` message with an `exited` payload and `turn_id` SET
  after *every single turn*; only a genuine process-level exit (turn_id is
  None, as ProcessDriver.on_exit() emits it) should finalize the session.
- "Verify-then-record" ordering for send_turn/answer_permission: the driver
  call happens first, and the corresponding user_input/approval_response
  message is only recorded if it doesn't raise.
"""

from __future__ import annotations

import threading

from drover.schema import bootstrap
import pytest

from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.structured import manager as manager_module
from drover.server.harness.structured.driver import StructuredMessage
from drover.server.harness.structured.manager import StructuredSessionManager


class _StubDriver:
    """Records calls; lets tests drive `emit()` directly to simulate wire
    traffic without spawning a real subprocess."""

    def __init__(self, command, cwd, emit):
        self.command = command
        self.cwd = cwd
        self.emit = emit
        self.started = False
        self.closed = False
        self.sent_turns: list[tuple[str, str]] = []
        self.sent_images: list = []
        self.answered: list[tuple[str, str, str | None]] = []
        self.send_turn_error: Exception | None = None
        self.answer_permission_error: Exception | None = None

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return not self.closed

    def send_turn(
        self,
        text: str,
        turn_id: str,
        images: list | None = None,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> None:
        del model, thinking_effort
        if self.send_turn_error is not None:
            raise self.send_turn_error
        self.sent_turns.append((text, turn_id))
        self.sent_images.append(images)

    def answer_permission(self, request_id, decision, note=None) -> None:
        if self.answer_permission_error is not None:
            raise self.answer_permission_error
        self.answered.append((request_id, decision, note))

    def interrupt(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


def _build_manager(monkeypatch, tmp_path, *, session_id: str = "sess-1"):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.create_session(
        host_id="test-host",
        harness="stub",
        command="stub",
        session_id=session_id,
        status="starting",
        mode="structured",
    )

    driver_holder: dict[str, _StubDriver] = {}

    def build(command, cwd, emit):
        driver = _StubDriver(command, cwd, emit)
        driver_holder["driver"] = driver
        return driver

    monkeypatch.setitem(manager_module._FACTORIES, "stub", (build, lambda: ["stub"]))

    mgr = StructuredSessionManager()
    on_messages: list[tuple[str, dict]] = []
    finalized: list[tuple[str, int]] = []
    mgr.start(
        session_id,
        harness="stub",
        cwd=None,
        command=None,
        registry=registry,
        on_message=lambda sid, evt: on_messages.append((sid, evt)),
        finalize=lambda sid, rc: finalized.append((sid, rc)),
    )
    return mgr, driver_holder["driver"], registry, on_messages, finalized


def test_finalize_only_triggers_on_turn_id_none_exit(monkeypatch, tmp_path):
    mgr, driver, registry, _on_messages, finalized = _build_manager(
        monkeypatch, tmp_path
    )

    # Codex/Gemini-style per-turn "exited" status: turn_id IS set. Must not
    # finalize -- otherwise every single turn would vanish the session.
    driver.emit(
        StructuredMessage(
            type="status",
            role="system",
            text="turn exited",
            payload={"exited": 1},
            turn_id="turn-abc",
        )
    )
    assert finalized == []
    session = registry.get_session("sess-1")
    assert session is not None
    assert session.status == "starting"

    # A genuine process-level exit (ProcessDriver.on_exit()'s shape: turn_id
    # is None) must finalize exactly once, with the right returncode.
    driver.emit(
        StructuredMessage(
            type="status",
            role="system",
            text="process exited",
            payload={"exited": 0},
            turn_id=None,
        )
    )
    assert finalized == [("sess-1", 0)]


def test_send_turn_dispatches_before_recording_and_skips_event_on_failure(
    monkeypatch, tmp_path
):
    mgr, driver, registry, on_messages, _finalized = _build_manager(
        monkeypatch, tmp_path
    )

    driver.send_turn_error = RuntimeError("turn already in flight")
    with pytest.raises(RuntimeError, match="turn already in flight"):
        mgr.send_turn("sess-1", "hello")

    assert driver.sent_turns == []
    assert not any(evt["type"] == "user_input" for _sid, evt in on_messages)
    assert not any(
        event.event_type == "user_input" for event in registry.list_events("sess-1")
    )

    driver.send_turn_error = None
    turn_id = mgr.send_turn("sess-1", "hello")

    assert driver.sent_turns == [("hello", turn_id)]
    user_input_events = [
        event
        for event in registry.list_events("sess-1")
        if event.event_type == "user_input"
    ]
    assert len(user_input_events) == 1
    assert user_input_events[0].payload["text"] == "hello"


def test_send_turn_forwards_images_and_records_attachments(monkeypatch, tmp_path):
    mgr, driver, registry, _on_messages, _finalized = _build_manager(
        monkeypatch, tmp_path
    )
    images = [{"path": "/tmp/a.png", "media_type": "image/png", "data_b64": "QUJD"}]
    turn_id = mgr.send_turn("sess-1", "see attached", images=images)

    assert driver.sent_turns == [("see attached", turn_id)]
    assert driver.sent_images == [images]

    user_input_events = [
        event
        for event in registry.list_events("sess-1")
        if event.event_type == "user_input"
    ]
    assert len(user_input_events) == 1
    # Attachment metadata is recorded, but never the base64 payload — events
    # are pushed to the hub and replayed into transcripts.
    assert user_input_events[0].payload["payload"]["attachments"] == [
        {"path": "/tmp/a.png", "media_type": "image/png"}
    ]


def test_answer_permission_dispatches_before_recording_and_skips_event_on_failure(
    monkeypatch, tmp_path
):
    mgr, driver, registry, _on_messages, _finalized = _build_manager(
        monkeypatch, tmp_path
    )

    driver.answer_permission_error = RuntimeError("no interactive approvals")
    with pytest.raises(RuntimeError, match="no interactive approvals"):
        mgr.answer_permission("sess-1", "req-1", "allow", None)

    assert driver.answered == []
    assert not any(
        event.event_type == "approval_response"
        for event in registry.list_events("sess-1")
    )

    driver.answer_permission_error = None
    mgr.answer_permission("sess-1", "req-1", "allow", "looks fine")

    assert driver.answered == [("req-1", "allow", "looks fine")]
    approval_events = [
        event
        for event in registry.list_events("sess-1")
        if event.event_type == "approval_response"
    ]
    assert len(approval_events) == 1
    assert approval_events[0].payload["payload"]["decision"] == "allow"


def test_awaiting_transitions_through_approval_and_input(monkeypatch, tmp_path):
    mgr, driver, _registry, _on_messages, _finalized = _build_manager(
        monkeypatch, tmp_path
    )

    assert mgr.awaiting("sess-1") is None

    driver.emit(
        StructuredMessage(
            type="approval_prompt",
            role="system",
            text="approval needed",
            payload={"request_id": "req-1"},
        )
    )
    assert mgr.awaiting("sess-1") == "approval"

    mgr.answer_permission("sess-1", "req-1", "allow", None)
    assert mgr.awaiting("sess-1") is None

    driver.emit(
        StructuredMessage(
            type="status",
            role="system",
            text="turn complete",
            payload={"turn_complete": True, "awaiting": "input"},
        )
    )
    assert mgr.awaiting("sess-1") == "input"


def test_seq_is_monotonic_across_emitted_messages(monkeypatch, tmp_path):
    mgr, driver, registry, _on_messages, _finalized = _build_manager(
        monkeypatch, tmp_path
    )

    for index in range(5):
        driver.emit(
            StructuredMessage(
                type="assistant_output", role="assistant", text=f"chunk {index}"
            )
        )

    events = registry.list_events("sess-1")
    seqs = [event.seq for event in events if event.seq is not None]
    assert seqs == list(range(1, len(seqs) + 1))


def test_concurrent_emits_from_two_threads_do_not_corrupt_registry_state(
    monkeypatch, tmp_path
):
    """Regression test for a real bug found via the daemon E2E test: emit()
    is called from multiple threads for the same session (a driver's own
    background pump thread, concurrently with an HTTP-handler thread calling
    send_turn/answer_permission, which synchronously emits the recorded
    message). Each registry write opens its own DuckDB connection
    (HarnessRegistry._connect()), so if two threads race into
    registry.append_event/update_session_activity for the same session_id
    at the same time, DuckDB raises a write-write TransactionException.
    entry.lock must serialize the whole side-effect sequence, not just the
    in-memory awaiting/seq bookkeeping.
    """
    mgr, driver, registry, _on_messages, _finalized = _build_manager(
        monkeypatch, tmp_path
    )

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def emit_many(prefix: str) -> None:
        try:
            barrier.wait(timeout=5)
            for index in range(25):
                driver.emit(
                    StructuredMessage(
                        type="assistant_output",
                        role="assistant",
                        text=f"{prefix}-{index}",
                    )
                )
        except Exception as exc:  # noqa: BLE001 - surfaced via errors list
            errors.append(exc)

    threads = [
        threading.Thread(target=emit_many, args=("a",)),
        threading.Thread(target=emit_many, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    events = registry.list_events("sess-1")
    seqs = sorted(event.seq for event in events if event.seq is not None)
    # No dropped/duplicated seq values despite concurrent emitters.
    assert seqs == list(range(1, len(seqs) + 1))
    assert len(seqs) == 50


def test_emit_survives_registry_write_failure(monkeypatch, tmp_path, capsys):
    """A registry failure inside emit() must never propagate: emit() runs on
    the driver's stdout-pump thread, and an escaped exception silently kills
    that thread, freezing the session (seen live with DuckDB's
    concurrent-connect BinderException before HarnessRegistry._connect() was
    serialized). The event must still reach on_message (central copy), and
    the stderr line must stay counts-only (no event text).
    """
    mgr, driver, registry, on_messages, _finalized = _build_manager(
        monkeypatch, tmp_path
    )

    original_append_event = registry.append_event

    def boom(**kwargs):
        raise RuntimeError("simulated duckdb failure")

    monkeypatch.setattr(registry, "append_event", boom)

    driver.emit(
        StructuredMessage(
            type="assistant_output", role="assistant", text="sensitive text"
        )
    )

    captured = capsys.readouterr()
    assert "registry write failed" in captured.err
    assert "RuntimeError" in captured.err
    assert "sensitive text" not in captured.err

    # on_message still ran for the failed-locally event.
    assert [evt["text"] for _sid, evt in on_messages] == ["sensitive text"]
    assert on_messages[0][1]["seq"] == 1

    # The session is not wedged: once the registry recovers, the next emit
    # records normally with the next seq.
    monkeypatch.setattr(registry, "append_event", original_append_event)
    driver.emit(
        StructuredMessage(type="assistant_output", role="assistant", text="next")
    )
    recorded = [
        event
        for event in registry.list_events("sess-1")
        if event.event_type == "assistant_output"
    ]
    assert [event.seq for event in recorded] == [2]


def test_finalize_still_fires_when_registry_write_fails(monkeypatch, tmp_path, capsys):
    """The survival guard must not swallow the finalize side effect: a
    genuine process-exit status (turn_id=None) whose registry write fails
    still has to finalize the session, or a crashed-during-db-outage
    session would linger as "running" forever.
    """
    mgr, driver, registry, _on_messages, finalized = _build_manager(
        monkeypatch, tmp_path
    )

    def boom(**kwargs):
        raise RuntimeError("simulated duckdb failure")

    monkeypatch.setattr(registry, "append_event", boom)

    driver.emit(
        StructuredMessage(
            type="status",
            role="system",
            text="process exited",
            payload={"exited": 0},
            turn_id=None,
        )
    )

    assert finalized == [("sess-1", 0)]
    captured = capsys.readouterr()
    assert "registry write failed" in captured.err


def test_emit_retries_then_counts_a_permanent_drop(monkeypatch, tmp_path):
    """emit() must never raise -- but it must not lose the event silently.

    The old handler made one attempt and swallowed the failure, so a
    transient DuckDB write-write conflict discarded the event forever.
    """
    from drover.server.harness import daemon as daemon_mod

    daemon_mod.reset_dropped_event_count()
    mgr, driver, registry, on_messages, _finalized = _build_manager(
        monkeypatch, tmp_path
    )

    attempts = {"n": 0}

    def boom(**kwargs):
        attempts["n"] += 1
        raise RuntimeError("TransactionException: write-write conflict")

    monkeypatch.setattr(registry, "append_event", boom)

    driver.emit(StructuredMessage(type="assistant_output", role="assistant", text="hi"))

    assert attempts["n"] == 3, "one attempt plus two retries"
    assert daemon_mod.dropped_event_count() == 1
    # The central copy must still go out -- that is the whole point of not
    # letting the local write failure propagate.
    assert on_messages, "on_message must still run after a failed local write"


def test_emit_retry_succeeds_without_counting_a_drop(monkeypatch, tmp_path):
    from drover.server.harness import daemon as daemon_mod

    daemon_mod.reset_dropped_event_count()
    mgr, driver, registry, _on_messages, _finalized = _build_manager(
        monkeypatch, tmp_path
    )

    original = registry.append_event
    attempts = {"n": 0}

    def flaky(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("TransactionException")
        return original(**kwargs)

    monkeypatch.setattr(registry, "append_event", flaky)

    driver.emit(StructuredMessage(type="assistant_output", role="assistant", text="hi"))

    assert attempts["n"] == 2
    assert daemon_mod.dropped_event_count() == 0
