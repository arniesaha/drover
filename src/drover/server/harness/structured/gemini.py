"""Gemini structured driver (per-turn respawn).

Wire shapes are grounded in ``tests/fixtures/structured/FINDINGS.md`` (Task
0's live probe of ``gemini`` 0.38.2). Read FINDINGS.md sec 3 before touching
this module -- the load-bearing caveat is:

**Gemini is UNAUTHENTICATED on this host.** No success-path capture exists.
Every live invocation failed immediately (exit code 41) with this JSON
envelope on *stderr* (not stdout):

    {"session_id": "...", "error": {"type": "Error", "message": "...",
     "code": 41}}

That error-envelope shape IS a live capture (committed as
``tests/fixtures/structured/gemini_basic.json``) and this driver's error
parsing is tested against those exact bytes. The success-path shape (a
``{"response": ..., "stats": {...}}`` JSON object on stdout, per
``gemini --help`` and the original brief) is **documented but unverified
pending auth** -- re-probe once ``GEMINI_API_KEY`` or an interactive OAuth
login is available on this host, and add a golden-fixture test alongside
``codex_basic.ndjson``/``claude_basic.ndjson`` at that point. Until then this
driver ships on fake-CLI tests only (``tests/test_structured_gemini.py``),
exactly as instructed.

UPDATE 2026-07-06 (gemini 0.46.0, authenticated via GEMINI_API_KEY): the
success envelope is now verified live and captured as
``tests/fixtures/structured/gemini_success.json`` — top-level ``session_id``,
``response`` (final text), and ``stats`` (per-model token/latency detail),
exactly the shape this driver assumed. New hard requirement discovered in the
same probe: headless runs in a not-yet-trusted directory exit 55 unless
``--skip-trust`` is passed (see ``_argv_for``).

Two more findings that diverge from the original brief:

1. **No persistent bidirectional process.** Like Codex, every turn is a
   fresh ``gemini -p <text> -o stream-json --approval-mode yolo`` subprocess
   per turn streaming NDJSON (verified live 2026-08-04, gemini 0.46.0,
   captured as ``gemini_stream.ndjson``) -- or one JSON error envelope on
   stderr and a nonzero exit, unchanged from the earlier ``-o json`` probe.
   ``GeminiDriver`` does not subclass ``ProcessDriver``.
2. **No resume support in v1.** FINDINGS.md sec 3 documents Gemini's
   ``-r/--resume <"latest"|index>`` flag as *index-based*, not id-based --
   unlike Claude's ``session_id``/Codex's ``thread_id``, which are opaque
   ids threaded straight back into a resume flag. Passing an index requires
   tracking session ordering/count rather than an opaque id, and the
   index-vs-id question for ``--resume`` was never verified live (the auth
   blocker prevented it). So this driver does **not** implement resume: each
   turn is context-free / respawned from scratch, and multi-turn
   conversational context does NOT carry over between ``send_turn`` calls
   until this is re-probed with working auth. The ``session_id`` field is
   present in the (captured) error envelope and is expected in the
   (unverified) success envelope too, but no ``_session_tag`` is stored or
   threaded into argv.
3. ``--approval-mode yolo`` is the only headless-safe choice (FINDINGS.md
   sec 3): ``default`` blocks forever waiting for a TTY approval prompt that
   never arrives in machine mode. ``answer_permission`` always raises --
   yolo mode auto-resolves every tool call, so there is no wire-level
   approval channel to answer into.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from typing import Any

from drover.server.harness.structured.driver import EmitFn, StructuredMessage

_STDERR_TAIL_LINES = 20


def default_command(binary: str | None = None) -> list[str]:
    return [binary or shutil.which("gemini") or "gemini"]


def _tail(text: str, n: int) -> str:
    lines = text.strip("\n").splitlines()
    return "\n".join(lines[-n:])


class GeminiDriver:
    """Owns one `gemini -p ... -o stream-json` subprocess per turn; no resume."""

    def __init__(self, command: list[str], cwd: str | None, emit: EmitFn) -> None:
        self.command = command
        self.cwd = cwd
        self.emit = emit
        # _turn_lock guards _turn_active/_turn_process/_turn_thread, mirroring
        # codex.py: a turn is "in flight" from send_turn setting _turn_active
        # (under the lock) until the worker's finally clears it -- never
        # inferred from Thread.is_alive(), which is False for a
        # created-but-not-started Thread and would let two interleaved
        # send_turn calls both pass. The Popen is also created inside
        # send_turn under the lock, BEFORE the worker starts, so
        # interrupt()/close() always see the live process handle whenever a
        # turn is in flight (no startup window).
        self._turn_lock = threading.Lock()
        self._turn_active = False
        self._turn_process: subprocess.Popen[str] | None = None
        self._turn_thread: threading.Thread | None = None
        self._closed = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self.emit(
            StructuredMessage(
                type="status",
                role="system",
                text="ready",
                payload={"awaiting": "input"},
            )
        )

    def is_alive(self) -> bool:
        return not self._closed

    def interrupt(self) -> None:
        with self._turn_lock:
            process = self._turn_process
        if process is not None and process.poll() is None:
            process.terminate()

    def close(self) -> None:
        self._closed = True
        with self._turn_lock:
            process = self._turn_process
            worker = self._turn_thread
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if worker is not None:
            worker.join(timeout=5)
        # If the join timed out (worker wedged mid-turn), re-check for a
        # still-live subprocess so nothing leaks past close().
        with self._turn_lock:
            process = self._turn_process
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    # -- turns -----------------------------------------------------------------

    def send_turn(self, text: str, turn_id: str) -> None:
        with self._turn_lock:
            if self._turn_active:
                raise RuntimeError("turn already in flight")
            if self._closed:
                raise RuntimeError("driver is closed")
            process = subprocess.Popen(
                self._argv_for(text),
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            worker = threading.Thread(
                target=self._run_turn, args=(process, turn_id), daemon=True
            )
            self._turn_active = True
            self._turn_process = process
            self._turn_thread = worker
            worker.start()

    def _argv_for(self, text: str) -> list[str]:
        # No resume flag: see the module docstring, point 2 -- every turn is
        # context-free in v1. --skip-trust is required since gemini 0.46:
        # headless runs in a not-yet-trusted cwd otherwise exit 55 with a
        # trusted-folders error (verified live 2026-07-06).
        return list(self.command) + [
            "-p",
            text,
            "-o",
            "stream-json",
            "--approval-mode",
            "yolo",
            "--skip-trust",
        ]

    def _run_turn(self, process: subprocess.Popen[str], turn_id: str) -> None:
        stderr_lines: list[str] = []

        def pump_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_lines.append(line.rstrip("\n"))

        stderr_thread = threading.Thread(target=pump_stderr, daemon=True)
        stderr_thread.start()
        delta_buffer: list[str] = []

        def flush_deltas() -> None:
            if not delta_buffer:
                return
            text = "".join(delta_buffer)
            delta_buffer.clear()
            self.emit(
                StructuredMessage(
                    type="assistant_output",
                    role="assistant",
                    text=text,
                    turn_id=turn_id,
                )
            )

        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                for message in self.parse_stream_line(line, delta_buffer, turn_id):
                    flush_deltas()
                    self.emit(message)
            flush_deltas()
            returncode = process.wait()
            stderr_thread.join(timeout=2)
        finally:
            with self._turn_lock:
                self._turn_process = None
                self._turn_active = False
        if returncode != 0:
            self.emit(
                self.parse_error(returncode, "\n".join(stderr_lines), turn_id=turn_id)
            )
            self.emit(
                StructuredMessage(
                    type="status",
                    role="system",
                    text="turn exited",
                    payload={"exited": returncode},
                    turn_id=turn_id,
                )
            )

    # -- parsing ---------------------------------------------------------------

    def parse_stream_line(
        self, line: str, delta_buffer: list[str], turn_id: str
    ) -> list[StructuredMessage]:
        try:
            obj: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            return [
                StructuredMessage(
                    type="raw",
                    role="system",
                    text=line,
                    payload={"stream": "stdout"},
                    turn_id=turn_id,
                )
            ]
        kind = obj.get("type")
        if kind == "message":
            if obj.get("role") == "assistant":
                delta_buffer.append(str(obj.get("content") or ""))
                return []
            # role=user is the CLI echoing the prompt back; the manager
            # already records a user_input event for every sent turn, so
            # emitting this would duplicate it in the transcript.
            return []
        if kind == "init":
            return [
                StructuredMessage(
                    type="status",
                    role="system",
                    text="init",
                    payload={"native_session_id": obj.get("session_id"), **obj},
                    turn_id=turn_id,
                )
            ]
        if kind == "tool_use":
            return [
                StructuredMessage(
                    type="tool_action",
                    role="assistant",
                    text=str(obj.get("tool_name") or ""),
                    payload={
                        "tool": obj.get("tool_name"),
                        "tool_use_id": obj.get("tool_id"),
                        "input": obj.get("parameters"),
                    },
                    turn_id=turn_id,
                )
            ]
        if kind == "tool_result":
            return [
                StructuredMessage(
                    type="tool_result",
                    role="tool",
                    text=str(obj.get("output") or obj.get("status") or ""),
                    payload={
                        "tool_use_id": obj.get("tool_id"),
                        "status": obj.get("status"),
                        **obj,
                    },
                    turn_id=turn_id,
                )
            ]
        if kind == "thought":
            # Defensive: never observed live (2026-08-04 probe, flash model);
            # gemini docs suggest thought summaries may stream in some modes.
            return [
                StructuredMessage(
                    type="assistant_output",
                    role="assistant",
                    text=str(obj.get("content") or obj.get("text") or ""),
                    payload={"thinking": True},
                    turn_id=turn_id,
                )
            ]
        if kind == "result":
            return [
                StructuredMessage(
                    type="status",
                    role="system",
                    text="turn complete",
                    payload={
                        "turn_complete": True,
                        "awaiting": "input",
                        "stats": obj.get("stats"),
                        "status": obj.get("status"),
                    },
                    turn_id=turn_id,
                )
            ]
        return [
            StructuredMessage(
                type="status",
                role="system",
                text=str(kind),
                payload=obj,
                turn_id=turn_id,
            )
        ]

    def parse_error(
        self, returncode: int, stderr_text: str, turn_id: str | None = None
    ) -> StructuredMessage:
        """Parse a nonzero-exit stderr blob as the captured error envelope.

        Live shape (FINDINGS.md sec 3, ``gemini_basic.json``):
        ``{"session_id": ..., "error": {"type", "message", "code"}}`` on
        stderr with empty stdout. Falls back to a raw stderr tail if the
        text isn't that shape (or isn't JSON at all).
        """
        tail = _tail(stderr_text, _STDERR_TAIL_LINES)
        envelope = self._try_parse_envelope(tail)
        if envelope is not None:
            error = envelope["error"]
            return StructuredMessage(
                type="error",
                role="system",
                text=error.get("message") or "",
                payload={
                    "code": error.get("code"),
                    "error_type": error.get("type"),
                    "session_id": envelope.get("session_id"),
                },
                turn_id=turn_id,
            )
        return StructuredMessage(
            type="error",
            role="system",
            text=tail or f"gemini exited with code {returncode}",
            payload={"returncode": returncode},
            turn_id=turn_id,
        )

    @staticmethod
    def _try_parse_envelope(text: str) -> dict[str, Any] | None:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
            return obj
        return None

    # -- approvals ---------------------------------------------------------------

    def answer_permission(
        self, request_id: str, decision: str, note: str | None = None
    ) -> None:
        del request_id, decision, note
        raise RuntimeError("gemini driver has no interactive approvals (yolo mode)")
