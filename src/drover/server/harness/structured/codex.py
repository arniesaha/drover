"""Codex structured driver (per-turn respawn).

Wire shapes are grounded in ``tests/fixtures/structured/FINDINGS.md`` (Task
0's live probe of ``codex-cli`` 0.142.4). Two load-bearing findings that
diverge from the original brief (see FINDINGS.md sec 2 and
``.superpowers/sdd/task-4-brief.md``'s header note, which the findings
supersede):

1. There is no ``codex proto`` subcommand in this build and no persistent
   bidirectional process the way Claude Code's ``stream-json`` mode works.
   Every turn is a fresh, short-lived ``codex exec --json`` (or
   ``codex exec resume <thread_id> --json`` for follow-ups) subprocess that
   streams NDJSON on stdout and exits once the turn completes. This mirrors
   the plan's Gemini driver shape, so ``CodexDriver`` does NOT subclass
   ``ProcessDriver`` -- it owns per-turn subprocesses directly.
2. ``codex exec`` (headless/non-interactive mode) has no wire-level
   approval channel at all: its default read-only sandbox silently denies
   disallowed operations and surfaces the failure to the model as a normal
   ``command_execution`` result, rather than emitting an
   ``exec_approval_request``-shaped event. Sandbox escalation flags (e.g.
   ``-s workspace-write``, ``-s danger-full-access``) are how a caller
   gets write access instead. This driver passes
   ``--sandbox danger-full-access`` on the first turn and the equivalent
   ``-c sandbox_mode=danger-full-access`` on resume turns (the ``resume``
   subcommand does not accept ``--sandbox``): the default resolved sandbox
   (workspace-write for trusted projects) keeps ``.git`` read-only and
   network off, which silently breaks every commit/push a session
   attempts, and with ``approval_policy: never`` there is no prompt to
   escalate through. Filesystem isolation is provided one level up by the
   daemon, which runs codex sessions in per-session git worktrees (see
   ``drover.server.harness.worktree``). ``answer_permission`` still always
   raises.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from drover.server.harness.structured.driver import EmitFn, StructuredMessage

_STDERR_TAIL_LINES = 20


def default_command(binary: str | None = None) -> list[str]:
    return [binary or shutil.which("codex") or "codex"]


def _catalog_number(value: Any) -> float | None:
    """Return a finite catalog number, rejecting booleans and malformed values."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def resolve_effective_context_window(
    model: str | None, *, codex_home: Path | None = None
) -> int | None:
    """Resolve Codex's runtime context limit from its local model catalog."""
    if not model:
        return None
    home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        catalog = json.loads((home / "models_cache.json").read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list):
        return None
    for entry in models:
        if not isinstance(entry, dict) or entry.get("slug") != model:
            continue
        context_window = _catalog_number(entry.get("context_window"))
        effective_percent = _catalog_number(
            entry.get("effective_context_window_percent")
        )
        if (
            context_window is None
            or context_window <= 0
            or effective_percent is None
            or not 0 < effective_percent <= 100
        ):
            return None
        return round(context_window * effective_percent / 100)
    return None


class CodexDriver:
    """Owns one `codex exec` subprocess per turn; no persistent process."""

    def __init__(
        self,
        command: list[str],
        cwd: str | None,
        emit: EmitFn,
        *,
        native_session_id: str | None = None,
        context_window_resolver: Callable[[str | None], int | None] | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.emit = emit
        self._thread_id = native_session_id
        self._context_window_resolver = (
            context_window_resolver or resolve_effective_context_window
        )
        # _turn_lock guards _turn_active/_turn_process/_turn_thread. A turn
        # is "in flight" from send_turn setting _turn_active (under the
        # lock) until the worker's finally clears it -- never inferred from
        # Thread.is_alive(), which is False for a created-but-not-started
        # Thread and would let two interleaved send_turn calls both pass.
        # The Popen is also created inside send_turn under the lock, BEFORE
        # the worker starts, so interrupt()/close() always see the live
        # process handle whenever a turn is in flight (no startup window).
        self._turn_lock = threading.Lock()
        self._turn_active = False
        self._turn_process: subprocess.Popen[str] | None = None
        self._turn_thread: threading.Thread | None = None
        self._turn_model: str | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
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

    def send_turn(
        self,
        text: str,
        turn_id: str,
        images: list | None = None,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> None:
        del images  # [Attached image: <path>] lines in the text are the channel here
        with self._turn_lock:
            if self._turn_active:
                raise RuntimeError("turn already in flight")
            if self._closed:
                raise RuntimeError("driver is closed")
            self._stderr_tail.clear()
            self._turn_model = model
            try:
                process = subprocess.Popen(
                    self._argv_for(text, model=model, thinking_effort=thinking_effort),
                    cwd=self.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except Exception:
                self._turn_model = None
                raise
            worker = threading.Thread(
                target=self._run_turn,
                args=(process, turn_id, self._turn_model),
                daemon=True,
            )
            self._turn_active = True
            self._turn_process = process
            self._turn_thread = worker
            worker.start()

    def _argv_for(
        self,
        text: str,
        *,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> list[str]:
        command = list(self.command)
        if model:
            command.extend(["--model", model])
        if thinking_effort:
            command.extend(["-c", f'model_reasoning_effort="{thinking_effort}"'])
        if self._thread_id is None:
            return command + [
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--sandbox",
                "danger-full-access",
                text,
            ]
        # ``codex exec resume`` does NOT accept ``--sandbox`` (that flag lives
        # only on the parent ``codex exec``); passing it aborts the follow-up
        # turn at arg-parse with "unexpected argument '--sandbox' found". The
        # config override is the supported equivalent on the resume path.
        return command + [
            "exec",
            "resume",
            self._thread_id,
            "--json",
            "--skip-git-repo-check",
            "-c",
            "sandbox_mode=danger-full-access",
            text,
        ]

    def _run_turn(
        self, process: subprocess.Popen[str], turn_id: str, model: str | None
    ) -> None:
        try:
            returncode = self._pump_turn(process, model=model)
        finally:
            with self._turn_lock:
                self._turn_process = None
                self._turn_active = False
                self._turn_model = None
        if returncode != 0:
            tail = "\n".join(self._stderr_tail)
            self.emit(
                StructuredMessage(
                    type="error",
                    role="system",
                    text=tail or f"codex exec exited with code {returncode}",
                    payload={"returncode": returncode},
                    turn_id=turn_id,
                )
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

    def _pump_turn(self, process: subprocess.Popen[str], *, model: str | None) -> int:
        stderr_thread = threading.Thread(
            target=self._pump_stderr, args=(process,), daemon=True
        )
        stderr_thread.start()
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            if not line.strip():
                continue  # blank lines are keepalive/noise, not output
            for message in self.parse_line(line, model=model):
                self.emit(message)
        returncode = process.wait()
        # Drain stderr fully before reading self._stderr_tail, so the error
        # message's stderr tail is complete rather than racy (same
        # discipline Task 2's ProcessDriver applies to its stdout pump).
        stderr_thread.join(timeout=2)
        return returncode

    def _pump_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            self._stderr_tail.append(line.rstrip("\n"))

    # -- parsing ---------------------------------------------------------------

    def parse_line(
        self, line: str, *, model: str | None = None
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
                )
            ]
        kind = obj.get("type")
        if kind == "thread.started":
            self._thread_id = obj.get("thread_id")
            return [
                StructuredMessage(
                    type="status",
                    role="system",
                    text="thread started",
                    payload={"native_session_id": obj.get("thread_id"), **obj},
                )
            ]
        if kind == "turn.started":
            return [
                StructuredMessage(type="status", role="system", text=kind, payload=obj)
            ]
        if kind == "item.started":
            return self._on_item_started(obj)
        if kind == "item.completed":
            return self._on_item_completed(obj)
        if kind == "turn.completed":
            payload: dict[str, Any] = {
                "turn_complete": True,
                "awaiting": "input",
                "usage": obj.get("usage"),
            }
            if model:
                payload["model"] = model
                try:
                    context_window = self._context_window_resolver(model)
                except Exception:
                    context_window = None
                if context_window is not None:
                    payload["model_context_window"] = context_window
            return [
                StructuredMessage(
                    type="status",
                    role="system",
                    text="turn complete",
                    payload=payload,
                )
            ]
        if isinstance(kind, str) and ("error" in kind or "failed" in kind):
            return [
                StructuredMessage(type="error", role="system", text=kind, payload=obj)
            ]
        # Any other valid-JSON event kind not enumerated above (protocol
        # drift) degrades to a typed status message, never to "raw" --
        # "raw" is reserved for lines that failed to parse as JSON at all.
        return [
            StructuredMessage(type="status", role="system", text=str(kind), payload=obj)
        ]

    def _on_item_started(self, obj: dict[str, Any]) -> list[StructuredMessage]:
        item = obj.get("item") or {}
        if item.get("type") == "command_execution":
            return [
                StructuredMessage(
                    type="tool_action",
                    role="assistant",
                    text=str(item.get("command") or ""),
                    payload={
                        "tool": "shell",
                        "tool_use_id": item.get("id"),
                        "input": {"command": item.get("command")},
                        **item,
                    },
                )
            ]
        return []  # e.g. agent_message item.started, if it ever occurs

    def _on_item_completed(self, obj: dict[str, Any]) -> list[StructuredMessage]:
        item = obj.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message":
            return [
                StructuredMessage(
                    type="assistant_output",
                    role="assistant",
                    text=item.get("text") or "",
                )
            ]
        if item_type == "reasoning":
            # Defensive: codex 0.144.4 never emits this in exec --json (even
            # with show_raw_agent_reasoning=true, verified live 2026-08-04),
            # but the protocol documents it and newer builds may.
            return [
                StructuredMessage(
                    type="assistant_output",
                    role="assistant",
                    text=item.get("text") or "",
                    payload={"thinking": True},
                )
            ]
        if item_type == "command_execution":
            output = item.get("aggregated_output") or ""
            return [
                StructuredMessage(
                    type="tool_result",
                    role="tool",
                    text=output,
                    payload={
                        "tool": "shell",
                        "tool_use_id": item.get("id"),
                        "exit_code": item.get("exit_code"),
                        "status": item.get("status"),
                        **item,
                    },
                )
            ]
        return [
            StructuredMessage(
                type="status", role="system", text=str(item_type), payload=obj
            )
        ]

    # -- approvals ---------------------------------------------------------------

    def answer_permission(
        self, request_id: str, decision: str, note: str | None = None
    ) -> None:
        del request_id, decision, note
        raise RuntimeError("codex exec has no approval channel; use sandbox flags")
