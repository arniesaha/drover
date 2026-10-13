"""Base plumbing for structured harness drivers.

A driver wraps one CLI subprocess running in its machine/JSON mode, parses
its stdout stream into normalized StructuredMessages, and hands each message
to an emit callback. Unparseable output degrades to type="raw" — never
dropped. Subclasses implement parse_line() plus CLI-specific turn/permission
writes via send_line().
"""

from __future__ import annotations

import json
import signal
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

MESSAGE_TYPES = frozenset(
    {
        "assistant_output",
        "user_input",
        "tool_action",
        "tool_result",
        "approval_prompt",
        "approval_response",
        "status",
        "error",
        "raw",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StructuredMessage:
    type: str
    role: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    turn_id: str | None = None
    event_id: str = field(default_factory=lambda: f"harness-event-{uuid4()}")
    ts: str = field(default_factory=_now_iso)

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "role": self.role,
            "text": self.text,
            "payload": self.payload,
            "turn_id": self.turn_id,
            "ts": self.ts,
        }


EmitFn = Callable[[StructuredMessage], None]

_STDERR_TAIL_LINES = 20


class ProcessDriver:
    """Owns one CLI subprocess; pumps stdout lines through parse_line."""

    def __init__(
        self,
        command: list[str],
        cwd: str | None,
        emit: EmitFn,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.emit = emit
        self.env = env
        self._process: subprocess.Popen[str] | None = None
        self._stdin_lock = threading.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._threads: list[threading.Thread] = []
        self._stderr_thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self.env,
        )
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stderr_thread.start()
        self._threads.append(self._stderr_thread)
        stdout_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        stdout_thread.start()
        self._threads.append(stdout_thread)

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def interrupt(self) -> None:
        if self.is_alive():
            assert self._process is not None
            self._process.send_signal(signal.SIGINT)

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        for thread in self._threads:
            thread.join(timeout=2)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    # -- I/O ---------------------------------------------------------------

    def send_line(self, obj: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeError("driver process is not running")
        line = json.dumps(obj)
        with self._stdin_lock:
            process.stdin.write(line + "\n")
            process.stdin.flush()

    # -- hooks for subclasses ----------------------------------------------

    def parse_line(self, line: str) -> list[StructuredMessage]:
        raise NotImplementedError

    def on_exit(self, returncode: int) -> list[StructuredMessage]:
        messages = [
            StructuredMessage(
                type="status",
                role="system",
                text="process exited",
                payload={"exited": returncode},
            )
        ]
        if returncode != 0:
            tail = "\n".join(self._stderr_tail)
            messages.insert(
                0,
                StructuredMessage(
                    type="error",
                    role="system",
                    text=tail or f"exited with code {returncode}",
                    payload={"returncode": returncode},
                ),
            )
        return messages

    # -- pumps ---------------------------------------------------------------

    def _emit_all(self, messages: list[StructuredMessage]) -> None:
        for message in messages:
            self.emit(message)

    def _pump_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            if not line.strip():
                continue  # intentional: blank lines are keepalive/noise, not output
            try:
                messages = self.parse_line(line)
            except Exception:  # noqa: BLE001 - protocol drift degrades to raw
                messages = [
                    StructuredMessage(
                        type="raw",
                        role="system",
                        text=line,
                        payload={"stream": "stdout"},
                    )
                ]
            self._emit_all(messages)
        returncode = process.wait()
        # Drain stderr fully before on_exit() reads self._stderr_tail, so the
        # error message's stderr tail is complete rather than racy.
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
        self._emit_all(self.on_exit(returncode))

    def _pump_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        for line in process.stderr:
            self._stderr_tail.append(line.rstrip("\n"))
