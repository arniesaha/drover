"""Cursor Agent CLI structured, per-turn driver.

Cursor's documented ``--print --output-format stream-json`` mode emits NDJSON
and is not a long-lived protocol.  A process is consequently owned per turn;
the CLI's ``--resume <session_id>`` preserves its native chat when available.
"""

from __future__ import annotations

from collections import deque
import json
import os
import shlex
import shutil
import subprocess
import threading
from typing import Any

from drover.server.harness.auth import redact_auth_text
from drover.server.harness.structured.driver import EmitFn, StructuredMessage

_STDERR_TAIL_LINES = 20


def default_command(binary: str | None = None) -> list[str]:
    """Return Cursor's installed compatibility entrypoint, if resolvable."""

    return [binary or shutil.which("cursor-agent") or "cursor-agent"]


def _tail(text: str, count: int = _STDERR_TAIL_LINES) -> str:
    return "\n".join(text.strip("\n").splitlines()[-count:])


def _message_text(value: object) -> str:
    """Collect documented text blocks without treating arbitrary JSON as text."""

    if not isinstance(value, dict):
        return ""
    content = value.get("content")
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def _tool_name_and_payload(value: object) -> tuple[str, dict[str, Any]]:
    """Cursor nests a tool call below its provider-specific tool name."""

    if not isinstance(value, dict):
        return "", {}
    for name, payload in value.items():
        if isinstance(name, str) and isinstance(payload, dict):
            return name, payload
    return "", {}


class CursorDriver:
    """Run one documented Cursor print-mode invocation for each Drover turn."""

    def __init__(
        self,
        command: list[str],
        cwd: str | None,
        emit: EmitFn,
        native_session_id: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = list(command)
        self.cwd = cwd
        self.emit = emit
        self.native_session_id = native_session_id
        self.env = {**os.environ, **(env or {})}
        self._turn_lock = threading.Lock()
        self._turn_active = False
        self._turn_process: subprocess.Popen[str] | None = None
        self._turn_thread: threading.Thread | None = None
        self._closed = False

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

    def has_turn_in_flight(self) -> bool:
        with self._turn_lock:
            worker = self._turn_thread
            return self._turn_active or (worker is not None and worker.is_alive())

    def interrupt(self) -> None:
        with self._turn_lock:
            process = self._turn_process
        if process is not None and process.poll() is None:
            process.terminate()

    def close(self) -> None:
        self._closed = True
        self.interrupt()
        with self._turn_lock:
            process = self._turn_process
            worker = self._turn_thread
        if worker is not None:
            worker.join(timeout=5)
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def send_turn(
        self,
        text: str,
        turn_id: str,
        images: list | None = None,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> None:
        # Cursor's reviewed CLI exposes no noninteractive attachment or
        # reasoning-effort option.  The adapter never advertises either.
        del images
        del thinking_effort
        with self._turn_lock:
            if self._turn_active:
                raise RuntimeError("turn already in flight")
            if self._closed:
                raise RuntimeError("driver is closed")
            process = subprocess.Popen(
                self._argv_for(text, model=model),
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self.env,
            )
            worker = threading.Thread(
                target=self._run_turn, args=(process, turn_id), daemon=True
            )
            self._turn_active = True
            self._turn_process = process
            self._turn_thread = worker
            worker.start()

    def _argv_for(self, text: str, *, model: str | None = None) -> list[str]:
        args: list[str] = []
        if self.native_session_id:
            args.extend(["--resume", self.native_session_id])
        if model:
            args.extend(["--model", model])
        # Do not add --force/--yolo/--trust or a sandbox override.  Those are
        # user-controlled Cursor permissions, not a Drover capability.
        args.extend(
            [
                "--print",
                "--output-format",
                "stream-json",
                "--stream-partial-output",
                text,
            ]
        )
        # Presets resolve CLIs through a login shell so launchd gets the same
        # Node/runtime environment.  Options must extend the shell command,
        # not become zsh positional parameters after ``-lc``.
        if len(self.command) >= 3 and self.command[1] == "-lc":
            command = list(self.command)
            command[2] += " " + shlex.join(args)
            return command
        return [*self.command, *args]

    def _run_turn(self, process: subprocess.Popen[str], turn_id: str) -> None:
        # Cursor can keep writing diagnostics while a network or login problem
        # persists. Keep only what can be shown in the terminal error rather
        # than retaining an unbounded amount of potentially sensitive output.
        stderr_lines: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)

        def pump_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_lines.append(line.rstrip("\n"))

        stderr_thread = threading.Thread(target=pump_stderr, daemon=True)
        stderr_thread.start()
        saw_result = False
        stream_failed = False
        returncode: int | None = None
        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                for message in self.parse_stream_line(line, turn_id):
                    if message.payload.get("turn_complete") is True:
                        saw_result = True
                    self.emit(message)
            returncode = process.wait()
        except Exception:
            # A decoder/read/parser/emit failure must not leave the child
            # running after Drover has forgotten its ownership. Deliberately
            # do not surface the exception: it can contain provider output.
            stream_failed = True
            returncode = self._terminate_and_reap(process)
        finally:
            if returncode is None:
                returncode = self._terminate_and_reap(process)
            stderr_thread.join(timeout=2)
            with self._turn_lock:
                self._turn_process = None
                self._turn_active = False

        if stream_failed:
            self.emit(
                StructuredMessage(
                    type="error",
                    role="system",
                    text="Cursor Agent stream processing failed",
                    payload={"stream_failure": True},
                    turn_id=turn_id,
                )
            )
            self.emit(
                StructuredMessage(
                    type="status",
                    role="system",
                    text="turn failed",
                    payload={
                        "turn_complete": True,
                        "awaiting": "input",
                        "error": True,
                        "stream_failure": True,
                    },
                    turn_id=turn_id,
                )
            )
            return

        if returncode == 0 and not saw_result:
            self.emit(
                StructuredMessage(
                    type="status",
                    role="system",
                    text="turn complete",
                    payload={
                        "turn_complete": True,
                        "awaiting": "input",
                        "missing_result": True,
                    },
                    turn_id=turn_id,
                )
            )
        elif returncode != 0:
            self.emit(self.parse_error(returncode, "\n".join(stderr_lines), turn_id))
            self.emit(
                StructuredMessage(
                    type="status",
                    role="system",
                    text="turn exited",
                    payload={"exited": returncode},
                    turn_id=turn_id,
                )
            )

    @staticmethod
    def _terminate_and_reap(process: subprocess.Popen[str]) -> int:
        """Return a child exit status after giving an abnormal stream a stop."""

        if process.poll() is None:
            process.terminate()
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                return process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # A process that cannot be reaped must still leave a stable
                # error path; Popen uses None while it remains unreaped.
                return process.returncode if process.returncode is not None else -1

    def parse_stream_line(self, line: str, turn_id: str) -> list[StructuredMessage]:
        try:
            obj = json.loads(line)
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
        if not isinstance(obj, dict):
            return [
                StructuredMessage(
                    type="raw",
                    role="system",
                    text=line,
                    payload={"stream": "stdout"},
                    turn_id=turn_id,
                )
            ]

        event_type = obj.get("type")
        session_id = obj.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            self.native_session_id = session_id

        if event_type == "system" and obj.get("subtype") == "init":
            return [
                StructuredMessage(
                    type="status",
                    role="system",
                    text="init",
                    payload={
                        "native_session_id": self.native_session_id,
                        "model": obj.get("model"),
                    },
                    turn_id=turn_id,
                )
            ]

        if event_type == "assistant":
            # With --stream-partial-output Cursor emits the new delta with a
            # timestamp and no model_call_id.  The pre-tool and final flushes
            # are duplicates and must not be replayed to users.
            if "timestamp_ms" not in obj or "model_call_id" in obj:
                return []
            text = _message_text(obj.get("message"))
            return (
                [
                    StructuredMessage(
                        type="assistant_output",
                        role="assistant",
                        text=text,
                        turn_id=turn_id,
                    )
                ]
                if text
                else []
            )

        if event_type == "tool_call":
            call_id = obj.get("call_id")
            call_id = call_id if isinstance(call_id, str) else ""
            name, tool_call = _tool_name_and_payload(obj.get("tool_call"))
            if obj.get("subtype") == "started":
                return [
                    StructuredMessage(
                        type="tool_action",
                        role="assistant",
                        text=name,
                        payload={
                            "tool": name,
                            "tool_use_id": call_id,
                            "input": tool_call.get("args"),
                        },
                        turn_id=turn_id,
                    )
                ]
            if obj.get("subtype") == "completed":
                result = tool_call.get("result")
                status = (
                    "success"
                    if isinstance(result, dict) and "success" in result
                    else "error"
                )
                return [
                    StructuredMessage(
                        type="tool_result",
                        role="tool",
                        text=json.dumps(result, ensure_ascii=False, default=str),
                        payload={
                            "tool_use_id": call_id,
                            "status": status,
                            "output": result,
                        },
                        turn_id=turn_id,
                    )
                ]
            return []

        if event_type == "result":
            successful = (
                obj.get("subtype") == "success" and obj.get("is_error") is False
            )
            if successful:
                return [
                    StructuredMessage(
                        type="status",
                        role="system",
                        text="turn complete",
                        payload={
                            "turn_complete": True,
                            "awaiting": "input",
                            "native_session_id": self.native_session_id,
                            "duration_ms": obj.get("duration_ms"),
                            "duration_api_ms": obj.get("duration_api_ms"),
                        },
                        turn_id=turn_id,
                    )
                ]
            detail = obj.get("result")
            text = (
                detail
                if isinstance(detail, str) and detail
                else "Cursor Agent reported a failed turn"
            )
            return [
                StructuredMessage(
                    type="error",
                    role="system",
                    text=redact_auth_text(text),
                    payload={"cursor_result": "error"},
                    turn_id=turn_id,
                ),
                StructuredMessage(
                    type="status",
                    role="system",
                    text="turn failed",
                    payload={
                        "turn_complete": True,
                        "awaiting": "input",
                        "error": True,
                        "cursor_result": "error",
                        "native_session_id": self.native_session_id,
                    },
                    turn_id=turn_id,
                ),
            ]

        return [
            StructuredMessage(
                type="status",
                role="system",
                text=str(event_type or "unknown"),
                payload={"event": event_type},
                turn_id=turn_id,
            )
        ]

    def parse_error(
        self, returncode: int, stderr_text: str, turn_id: str | None = None
    ) -> StructuredMessage:
        detail = redact_auth_text(_tail(stderr_text))
        return StructuredMessage(
            type="error",
            role="system",
            text=detail or f"Cursor Agent exited with code {returncode}",
            payload={"exit_code": returncode},
            turn_id=turn_id,
        )
