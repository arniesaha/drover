"""Owns live structured-session drivers for one harnessd instance.

Each entry pumps every driver-emitted ``StructuredMessage`` through a
per-session ``seq`` counter into the registry, tracks the derived
``awaiting`` state, and forwards the event to an ``on_message`` callback
(the Task 7 event pusher). See ``send_turn``/``answer_permission`` for the
"verify-then-record" ordering: the driver call happens first, and the
corresponding ``user_input``/``approval_response`` message is only recorded
after it succeeds, so a rejected call (e.g. "turn already in flight", or
Codex/Agy's unconditional approval-channel ``RuntimeError``) never
leaves a phantom event in the registry.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.structured import agy, claude, codex
from drover.server.harness.structured.driver import StructuredMessage

# Each factory is a small builder, not a bare class -- ClaudeDriver needs a
# sanitized child environment (claude.child_env() strips ambient CLAUDE*
# vars so a nested harnessd doesn't leak its own session env into the
# spawned CLI); Codex/Agy's constructors take no env kwarg at all.
_FACTORIES: dict[str, tuple[Callable[..., Any], Callable[..., list[str]]]] = {
    "claude-code": (
        lambda command, cwd, emit, native_session_id: claude.ClaudeDriver(
            claude.resume_command(command, native_session_id),
            cwd,
            emit,
            env=claude.child_env(),
        ),
        claude.default_command,
    ),
    "codex": (
        lambda command, cwd, emit, native_session_id: codex.CodexDriver(
            command, cwd, emit, native_session_id=native_session_id
        ),
        codex.default_command,
    ),
    "agy": (
        lambda command, cwd, emit, native_session_id: agy.AgyDriver(
            agy.resume_command(command, native_session_id),
            cwd,
            emit,
            native_session_id=native_session_id,
        ),
        agy.default_command,
    ),
}


class _Entry:
    def __init__(self, driver: Any, harness: str) -> None:
        self.driver = driver
        self.harness = harness
        self.seq = 0
        self.awaiting: str | None = None
        self.lock = threading.Lock()
        self.turn_lock = threading.Lock()
        self.turn_active = False
        self.emit: Callable[[StructuredMessage], None] | None = None


class StructuredSessionManager:
    """Thread-safe registry of live structured-session driver instances."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._entries_lock = threading.Lock()

    def has(self, session_id: str) -> bool:
        with self._entries_lock:
            return session_id in self._entries

    def is_alive(self, session_id: str) -> bool:
        with self._entries_lock:
            entry = self._entries.get(session_id)
        return bool(entry and entry.driver.is_alive())

    def harness_for(self, session_id: str) -> str | None:
        with self._entries_lock:
            entry = self._entries.get(session_id)
        return entry.harness if entry else None

    def awaiting(self, session_id: str) -> str | None:
        with self._entries_lock:
            entry = self._entries.get(session_id)
        return entry.awaiting if entry else None

    def session_ids(self) -> list[str]:
        with self._entries_lock:
            return list(self._entries.keys())

    def start(
        self,
        session_id: str,
        *,
        harness: str,
        cwd: str | None,
        command: list[str] | None,
        registry: HarnessRegistry,
        on_message: Callable[[str, dict[str, Any]], None],
        finalize: Callable[[str, int], None],
        native_session_id: str | None = None,
    ) -> None:
        if harness not in _FACTORIES:
            raise ValueError(f"harness has no structured driver: {harness}")
        builder, default_command = _FACTORIES[harness]
        entry = _Entry(None, harness)
        entry.seq = registry.max_event_seq(session_id)

        def emit(message: StructuredMessage) -> None:
            payload = message.payload or {}
            # Only a genuine process-level exit ends this entry's life.
            # ProcessDriver.on_exit() leaves turn_id=None on its "process
            # exited" status message, but Codex/Agy's per-turn respawn
            # drivers emit an "exited" status with turn_id SET after every
            # single turn -- gating on turn_id is None keeps those from
            # prematurely finalizing (and vanishing from listings) after
            # their first turn.
            process_exited = (
                message.type == "status"
                and "exited" in payload
                and message.turn_id is None
            )
            # Discard BEFORE recording the event, not after. The exit event is
            # what makes the session look finished to everything else --
            # recovery reads the registry and then asks the manager -- so an
            # entry still listed as live at that moment is exactly the dead
            # entry recovery must never mistake for an idempotently recovered
            # session. Discarding afterwards left that window open for the
            # whole duration of the write, and the write contends for the
            # registry's process-wide connect lock with every other session's
            # pump thread, so it is not a micro-window.
            if process_exited:
                self._discard_entry(session_id, entry)
            # entry.lock serializes the ENTIRE per-message side effect
            # sequence, not just the awaiting-state mutation: emit() runs
            # from multiple threads for the same session (a driver's own
            # stdout-pump thread emitting wire messages, concurrently with
            # an HTTP-handler thread calling send_turn/answer_permission,
            # which synchronously emits the recorded user_input/
            # approval_response message). Two threads calling
            # registry.append_event/update_session_activity for the same
            # session_id at the same time -- each on its own DuckDB
            # connection -- can hit a write-write TransactionException, so
            # the registry writes must be inside the same lock that
            # serializes seq/awaiting.
            with entry.lock:
                entry.seq += 1
                seq = entry.seq
                if message.type == "approval_prompt":
                    entry.awaiting = "approval"
                elif message.type == "approval_response":
                    entry.awaiting = None
                elif message.type == "status" and payload.get("awaiting") == "input":
                    entry.awaiting = "input"
                elif message.type == "user_input":
                    entry.awaiting = None
                if message.type == "status" and (
                    payload.get("turn_complete") or "exited" in payload
                ):
                    with entry.turn_lock:
                        entry.turn_active = False
                awaiting = entry.awaiting
                event_payload = message.to_payload()
                event_payload["seq"] = seq
                event_payload["session_id"] = session_id
                # Imported here, not at module scope: daemon imports this
                # module, so a top-level import would be circular.
                from drover.server.harness.daemon import record_dropped_events

                recorded = False
                for attempt in range(3):
                    try:
                        native_session_id = payload.get("native_session_id")
                        if (
                            isinstance(native_session_id, str)
                            and native_session_id.strip()
                        ):
                            registry.update_session_native_id(
                                session_id, native_session_id
                            )
                        registry.append_event(
                            session_id=session_id,
                            event_type=message.type,
                            payload=event_payload,
                            seq=seq,
                            harness=harness,
                            normalized_source="structured",
                        )
                        registry.update_session_activity(session_id, awaiting=awaiting)
                        recorded = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        # A registry failure must never propagate: emit()
                        # runs on the driver's stdout-pump thread, and an
                        # escaped exception silently kills that thread,
                        # freezing the session (seen live with DuckDB's
                        # concurrent-connect BinderException before
                        # HarnessRegistry._connect() was serialized).
                        # Write-write conflicts are transient, so retry
                        # before giving up.
                        if attempt < 2:
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        # Counts only -- no event text, it may contain
                        # sensitive content. on_message still runs below,
                        # so the central copy can still succeed.
                        print(
                            "drover structured manager: registry write failed "
                            f"for session {session_id} seq {seq} "
                            f"({type(exc).__name__}); event not recorded "
                            "locally",
                            file=sys.stderr,
                        )
                if not recorded:
                    record_dropped_events(1)
            on_message(session_id, event_payload)
            # Finalize stays after the write: it records the exit code against
            # a session whose last event has already landed.
            if process_exited:
                finalize(session_id, int(payload["exited"]))

        entry.emit = emit
        entry.driver = builder(
            command or default_command(), cwd, emit, native_session_id
        )
        with self._entries_lock:
            self._entries[session_id] = entry
        entry.driver.start()

    def record_recovered(self, session_id: str, native_session_id: str) -> None:
        entry = self._require_entry(session_id)
        assert entry.emit is not None
        entry.emit(
            StructuredMessage(
                type="session.recovered",
                role="system",
                text="session recovered after harness restart",
                payload={"native_session_id": native_session_id},
            )
        )

    def send_turn(
        self,
        session_id: str,
        text: str,
        images: list | None = None,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> str:
        entry = self._require_entry(session_id)
        if entry.awaiting == "approval":
            raise PermissionError("approval pending; answer it first")
        guard_persistent_turn = entry.harness == "claude-code"
        if guard_persistent_turn:
            with entry.turn_lock:
                if entry.turn_active:
                    raise RuntimeError("turn already in flight")
                entry.turn_active = True
        turn_id = f"turn-{uuid4()}"
        # Dispatch first: Codex/Agy raise RuntimeError here ("turn
        # already in flight" / "driver is closed") when a turn can't be
        # accepted, and we must not record a user_input event for a turn
        # that was never actually sent.
        try:
            entry.driver.send_turn(
                text,
                turn_id,
                images=images,
                model=model,
                thinking_effort=thinking_effort,
            )
        except Exception:
            if guard_persistent_turn:
                with entry.turn_lock:
                    entry.turn_active = False
            raise
        payload: dict = {}
        if images:
            # Metadata only — the base64 payload never enters the event
            # stream (events are pushed to the hub and replayed later).
            payload["attachments"] = [
                {"path": image["path"], "media_type": image["media_type"]}
                for image in images
            ]
        entry.driver.emit(
            StructuredMessage(
                type="user_input",
                role="user",
                text=text,
                turn_id=turn_id,
                payload=payload,
            )
        )
        return turn_id

    def answer_permission(
        self, session_id: str, request_id: str, decision: str, note: str | None
    ) -> None:
        entry = self._require_entry(session_id)
        # Dispatch first: Codex/Agy always raise RuntimeError here (no
        # wire-level approval channel), and we must not record a phantom
        # approval_response event for a driver that rejected it.
        entry.driver.answer_permission(request_id, decision, note)
        entry.driver.emit(
            StructuredMessage(
                type="approval_response",
                role="user",
                text=decision,
                payload={
                    "request_id": request_id,
                    "decision": decision,
                    "note": note,
                },
            )
        )

    def interrupt(self, session_id: str) -> None:
        self._require_entry(session_id).driver.interrupt()

    def close(self, session_id: str) -> None:
        with self._entries_lock:
            entry = self._entries.pop(session_id, None)
        if entry is not None:
            entry.driver.close()

    def _require_entry(self, session_id: str) -> _Entry:
        with self._entries_lock:
            entry = self._entries.get(session_id)
        if entry is None:
            raise KeyError(f"unknown structured session {session_id!r}")
        return entry

    def _discard_entry(self, session_id: str, expected: _Entry) -> None:
        with self._entries_lock:
            if self._entries.get(session_id) is expected:
                self._entries.pop(session_id, None)
