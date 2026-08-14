"""Bounded JSONL transport for a local Codex app-server process."""

from __future__ import annotations

import json
import logging
from queue import Empty, Full, Queue
import subprocess
import threading
from time import monotonic
from typing import Any, Mapping, Sequence

log = logging.getLogger(__name__)
_PROCESS_STOP_TIMEOUT_S = 0.5
_MAX_CAPTURED_STDERR_CHARS = 16_384
_MAX_STDOUT_LINE_BYTES = 1_048_576
_MAX_PENDING_STDOUT_LINES = 4
_STDOUT_PROTOCOL_ERROR = object()


class CodexAppServerError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class CodexAppServerSession:
    """A bounded session with a local Codex app-server process."""

    def __init__(self, command: Sequence[str], timeout_s: float):
        self.command = tuple(command)
        self.timeout_s = timeout_s
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._lines: Queue[object] = Queue(maxsize=_MAX_PENDING_STDOUT_LINES)
        self._stderr_parts: list[str] = []
        self._deadline: float | None = None
        self._next_request_id = 1

    def __enter__(self) -> "CodexAppServerSession":
        try:
            self._start()
            initialized = self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "drover",
                        "title": "Drover",
                        "version": "0.2.0",
                    }
                },
            )
            if not isinstance(initialized, Mapping):
                raise CodexAppServerError("protocol_error")
            self.notify("initialized", {})
            return self
        except Exception:
            self._cleanup()
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._cleanup()

    def request(
        self, method: str, params: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        return self._request_with_id(request_id, method, params)

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def _start(self) -> None:
        if self.timeout_s <= 0:
            raise CodexAppServerError("timeout")
        self._deadline = monotonic() + self.timeout_s
        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            raise CodexAppServerError("cli_not_found") from None
        except OSError:
            raise CodexAppServerError("unavailable") from None

        self._process = process
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise CodexAppServerError("process_error")
        self._reader = _stdout_reader(process.stdout.buffer, self._lines)
        self._stderr_reader = _stderr_reader(process.stderr, self._stderr_parts)

    def _request_with_id(
        self,
        request_id: int,
        method: str,
        params: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)
        while True:
            deadline = self._deadline
            if deadline is None:
                raise CodexAppServerError("process_error")
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CodexAppServerError("timeout")
            try:
                line = self._lines.get(timeout=remaining)
            except Empty:
                raise CodexAppServerError("timeout") from None
            if line is _STDOUT_PROTOCOL_ERROR:
                raise CodexAppServerError("protocol_error")
            if line is None:
                raise CodexAppServerError("process_error")
            if not isinstance(line, bytes):
                raise CodexAppServerError("protocol_error")
            try:
                response = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                raise CodexAppServerError("protocol_error") from None
            if not isinstance(response, Mapping):
                raise CodexAppServerError("protocol_error")
            if response.get("id") != request_id:
                continue
            if "error" in response or not isinstance(response.get("result"), Mapping):
                raise CodexAppServerError("protocol_error")
            return response["result"]

    def _write(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CodexAppServerError("process_error")
        try:
            process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise CodexAppServerError("process_error") from None

    def _cleanup(self) -> None:
        try:
            if self._process is not None:
                _stop_process(self._process)
            if self._reader is not None:
                self._reader.join(timeout=_PROCESS_STOP_TIMEOUT_S)
            if self._stderr_reader is not None:
                self._stderr_reader.join(timeout=_PROCESS_STOP_TIMEOUT_S)
            stderr = "".join(self._stderr_parts)
            if stderr:
                # Stderr can contain CLI or auth diagnostics. It is never
                # returned in an API response and must be redacted before a
                # local diagnostic log sees it.
                from drover.server.harness.auth import redact_auth_text

                log.debug("codex app-server probe stderr: %s", redact_auth_text(stderr))
        except Exception:
            log.debug("codex app-server cleanup failed", exc_info=True)


def _stdout_reader(stream, lines: Queue[object]) -> threading.Thread:
    def read_lines() -> None:
        try:
            while True:
                line = stream.readline(_MAX_STDOUT_LINE_BYTES + 1)
                if line == b"":
                    break
                if len(line) > _MAX_STDOUT_LINE_BYTES or not line.endswith(b"\n"):
                    _signal_stdout_protocol_error(lines)
                    return
                try:
                    lines.put(line, timeout=_PROCESS_STOP_TIMEOUT_S)
                except Full:
                    _signal_stdout_protocol_error(lines)
                    return
        finally:
            try:
                lines.put_nowait(None)
            except Full:
                pass

    thread = threading.Thread(target=read_lines, daemon=True)
    thread.start()
    return thread


def _signal_stdout_protocol_error(lines: Queue[object]) -> None:
    try:
        lines.put_nowait(_STDOUT_PROTOCOL_ERROR)
        return
    except Full:
        pass
    try:
        lines.get_nowait()
    except Empty:
        pass
    try:
        lines.put_nowait(_STDOUT_PROTOCOL_ERROR)
    except Full:
        pass


def _stderr_reader(stream, captured: list[str]) -> threading.Thread:
    def read_chunks() -> None:
        remaining = _MAX_CAPTURED_STDERR_CHARS
        for chunk in iter(lambda: stream.read(4096), ""):
            if remaining:
                captured.append(chunk[:remaining])
                remaining -= len(chunk)

    thread = threading.Thread(target=read_chunks, daemon=True)
    thread.start()
    return thread


def _stop_process(process: subprocess.Popen[str]) -> None:
    """Stop the child, and never raise while doing it."""
    try:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=_PROCESS_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=_PROCESS_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                log.debug("codex app-server survived SIGKILL; abandoning it")
    except OSError:
        log.debug("codex app-server could not be stopped", exc_info=True)
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
