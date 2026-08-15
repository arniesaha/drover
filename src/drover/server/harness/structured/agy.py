"""Antigravity CLI (agy) structured driver.

Runs `agy --dangerously-skip-permissions --output-format stream-json --print <text>`
as a per-turn process with --conversation <id> session resumption.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
from typing import Any

from drover.server.harness.structured.driver import EmitFn, StructuredMessage

log = logging.getLogger("drover.harnessd")

_STDERR_TAIL_LINES = 20

# How long a turn may run before agy ends it itself.
#
# `agy --print` defaults to 5m0s, which is far shorter than a coding turn: on
# 2026-08-15 three turns died at exactly that mark, each still waiting on a
# `run_command` that had not returned (issue #188). agy reported `status:
# ERROR` in its result event and exited 1 with an empty stderr, so from the
# app the turn looked like it had completed and then failed for no reason.
#
# Large rather than unlimited. The cap is not what protects the fleet -- a
# session can be terminated, and the daemon owns the process either way -- but
# a turn whose process is genuinely wedged should not outlive the day.
AGY_PRINT_TIMEOUT = "12h"


def default_command(binary: str | None = None) -> list[str]:
    return [binary or shutil.which("agy") or "agy"]


def resume_command(command: list[str], native_session_id: str | None) -> list[str]:
    resumed = list(command)
    if native_session_id:
        resumed.extend(["--conversation", native_session_id])
    return resumed


def _tail(text: str, n: int) -> str:
    lines = text.strip("\n").splitlines()
    return "\n".join(lines[-n:])


class AgyDriver:
    """Owns an `agy --output-format stream-json --print` process per turn."""

    def __init__(
        self,
        command: list[str],
        cwd: str | None,
        emit: EmitFn,
        native_session_id: str | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.emit = emit
        self.native_session_id = native_session_id
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
        with self._turn_lock:
            process = self._turn_process
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
            )
            worker = threading.Thread(
                target=self._run_turn, args=(process, turn_id), daemon=True
            )
            self._turn_active = True
            self._turn_process = process
            self._turn_thread = worker
            worker.start()

    def _argv_for(self, text: str, *, model: str | None = None) -> list[str]:
        command = list(self.command)
        if "--dangerously-skip-permissions" not in command:
            command.append("--dangerously-skip-permissions")
        # agy ignores the process cwd -- it resolves its workspace from
        # --add-dir/--project and otherwise falls back to
        # ~/.gemini/antigravity-cli/scratch, so Popen(cwd=...) alone leaves
        # the session pointed at the scratch dir.
        if self.cwd and "--add-dir" not in command:
            command.extend(["--add-dir", self.cwd])
        if model and "--model" not in command:
            command.extend(["--model", model])
        if self.native_session_id and "--conversation" not in command:
            command.extend(["--conversation", self.native_session_id])
        if "--print-timeout" not in command:
            command.extend(["--print-timeout", AGY_PRINT_TIMEOUT])
        command.extend(["--output-format", "stream-json", "--print", text])
        return command

    def _run_turn(self, process: subprocess.Popen[str], turn_id: str) -> None:
        stderr_lines: list[str] = []

        def pump_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_lines.append(line.rstrip("\n"))

        stderr_thread = threading.Thread(target=pump_stderr, daemon=True)
        stderr_thread.start()
        delta_buffer: list[str] = []
        saw_result = False
        reported_status: str | None = None

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
                    if (
                        message.type == "status"
                        and message.payload.get("turn_complete") is True
                    ):
                        saw_result = True
                        # agy states its own verdict here. It was being kept
                        # in the payload and never shown, so a turn it had
                        # already called ERROR was rendered as "turn complete"
                        # followed by a bare exit code (#188).
                        reported_status = message.payload.get("status")
                    self.emit(message)
            flush_deltas()
            returncode = process.wait()
            stderr_thread.join(timeout=2)
        finally:
            with self._turn_lock:
                self._turn_process = None
                self._turn_active = False

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
        if returncode != 0:
            # Recorded whether or not anyone is looking at the app. Nothing on
            # the hub noted these at all, so a failure the user did not
            # screenshot was unrecoverable (#188).
            log.warning(
                "agy exited with code %s (turn %s, after_turn_complete=%s, "
                "reported status %s)",
                returncode,
                turn_id,
                saw_result,
                reported_status or "none",
            )
            self.emit(
                self.parse_error(
                    returncode,
                    "\n".join(stderr_lines),
                    turn_id=turn_id,
                    after_turn_complete=saw_result,
                    reported_status=reported_status,
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

        event = obj.get("event")
        if event == "init":
            init = obj.get("init") or {}
            conv_id = obj.get("conversation_id") or init.get("conversation_id")
            if conv_id:
                self.native_session_id = str(conv_id)
            return [
                StructuredMessage(
                    type="status",
                    role="system",
                    text="init",
                    payload={"native_session_id": conv_id, **obj},
                    turn_id=turn_id,
                )
            ]

        if event == "step_update":
            update = obj.get("step_update") or {}
            step_type = update.get("step_type")
            if step_type == "agent_response":
                text_delta = update.get("text_delta")
                if text_delta:
                    delta_buffer.append(str(text_delta))
                return []
            if step_type == "tool":
                # `or {}` rather than a `{}` default: a present-but-null
                # `tool_info` would otherwise raise here, and this runs on the
                # turn thread where an escaping exception kills the pump
                # without ever emitting a turn-complete.
                tool_info = update.get("tool_info") or {}
                tool_name = update.get("tool_name") or tool_info.get("name")
                state = update.get("state")
                output = tool_info.get("output")
                messages = []
                if state == "ACTIVE" or output is None:
                    messages.append(
                        StructuredMessage(
                            type="tool_action",
                            role="assistant",
                            text=str(tool_name or ""),
                            payload={
                                "tool": tool_name,
                                "tool_use_id": f"tool-{update.get('step_index')}",
                                "input": tool_info.get("parameters"),
                            },
                            turn_id=turn_id,
                        )
                    )
                if state == "DONE" and output is not None:
                    messages.append(
                        StructuredMessage(
                            type="tool_result",
                            role="tool",
                            text=str(output),
                            payload={
                                "tool_use_id": f"tool-{update.get('step_index')}",
                                "status": "success",
                                "output": output,
                            },
                            turn_id=turn_id,
                        )
                    )
                return messages
            return []

        if event == "result":
            res = obj.get("result") or {}
            conv_id = res.get("conversation_id")
            if conv_id:
                self.native_session_id = str(conv_id)
            return [
                StructuredMessage(
                    type="status",
                    role="system",
                    text="turn complete",
                    payload={
                        "turn_complete": True,
                        "awaiting": "input",
                        "stats": res.get("usage"),
                        "status": res.get("status"),
                        "native_session_id": conv_id,
                    },
                    turn_id=turn_id,
                )
            ]

        return [
            StructuredMessage(
                type="status",
                role="system",
                text=str(event or "unknown"),
                payload=obj,
                turn_id=turn_id,
            )
        ]

    def parse_error(
        self,
        returncode: int,
        stderr_text: str,
        turn_id: str | None = None,
        *,
        after_turn_complete: bool = False,
        reported_status: str | None = None,
    ) -> StructuredMessage:
        """Describe a non-zero exit in the terms the user has to decide in.

        A process that failed *after* delivering a turn leaves the session
        usable and the work done; one that failed before delivering anything
        lost the turn. Both used to render as the same bubble, and since agy
        writes nothing to stderr on either path, that bubble was the exit code
        alone (#188).
        """

        tail = _tail(stderr_text, _STDERR_TAIL_LINES)
        if tail:
            text = tail
        elif after_turn_complete:
            text = f"agy exited with code {returncode} after completing the turn"
            if reported_status:
                text += f", reporting status {reported_status}"
            # The deadline is the one cause seen in the wild, and the one a
            # user can act on -- everything else about the exit is invisible.
            text += (
                f". The turn's work is already in the transcript; a turn that "
                f"runs past {AGY_PRINT_TIMEOUT} is ended by agy itself."
            )
        else:
            text = f"agy exited with code {returncode}"
        return StructuredMessage(
            type="error",
            role="system",
            text=text,
            payload={
                "returncode": returncode,
                "after_turn_complete": after_turn_complete,
                "reported_status": reported_status,
            },
            turn_id=turn_id,
        )

    def answer_permission(
        self, request_id: str, decision: str, note: str | None = None
    ) -> None:
        del request_id, decision, note
        raise RuntimeError(
            "agy driver has no interactive approvals (auto-approve mode)"
        )
