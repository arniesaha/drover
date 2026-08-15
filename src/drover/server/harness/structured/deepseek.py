"""Structured DeepSeek Harness driver over its local Web RPC API.

DeepSeek Harness already owns durable, event-sourced sessions.  Drover talks
to that boundary instead of spawning the one-shot ``headless`` profile, which
would discard native continuity after every turn.  The Web service remains
loopback-only by default; override ``DROVER_DEEPSEEK_HARNESS_URL`` when a host
uses a different local address.

Turn correlation.  Nothing in the RPC surface this adapter consumes names a
turn: ``session.prompt`` acknowledges the submission without returning an
identifier, and a ``session.history`` item is ``{"event": {"type", "seq",
"surfaceOp", "data"}, "view": ...}`` -- a per-session monotonic ``seq`` and no
turn field on any of the event types handled here.  Correlation is therefore
positional: the cursor is fenced to the end of the existing history
immediately before the prompt is submitted, and everything after that fence
belongs to the turn being sent.  If a future ``dsh`` does stamp a turn ID on
its events, that identity should replace this fence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from drover.server.harness.structured.driver import EmitFn, StructuredMessage
from drover.server.harness.structured.workspace import (
    probe_workspace,
    require_workspace_root,
    workspace_messages,
)

DEFAULT_API_URL = "http://127.0.0.1:3080"

# ``session.history`` exposes a tail window (``maxMessages``) and a ``hasMore``
# flag; it has no cursor or offset parameter. A busy turn can therefore write
# more messages between two polls than the default window holds, so the window
# is widened until it overlaps what has already been consumed. The bound keeps
# a pathological backlog (or an API that always claims ``hasMore``) from
# spinning: at most _HISTORY_MAX_MESSAGES messages are ever requested.
_HISTORY_TAIL_MESSAGES = 4
_HISTORY_MAX_MESSAGES = 512
_HISTORY_WINDOW_GROWTH = 4


def default_command(binary: str | None = None) -> list[str]:
    """Return the CLI used as the host capability/discovery gate."""
    return [binary or shutil.which("dsh") or "dsh"]


class DeepSeekApiError(RuntimeError):
    pass


class DeepSeekApiClient:
    def __init__(self, base_url: str | None = None, timeout_s: float = 10.0):
        self.base_url = (
            base_url or os.environ.get("DROVER_DEEPSEEK_HARNESS_URL") or DEFAULT_API_URL
        ).rstrip("/")
        self.timeout_s = timeout_s

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        rpc_id = f"drover-{uuid4()}"
        body = json.dumps(
            {
                "type": "client-request",
                "rpcId": rpc_id,
                "method": method,
                "payload": payload,
            }
        ).encode()
        request = Request(
            f"{self.base_url}/api/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                value = json.load(response)
        except (
            HTTPError,
            URLError,
            OSError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            raise DeepSeekApiError(f"DeepSeek Harness API unavailable: {exc}") from exc
        if not isinstance(value, dict) or value.get("rpcId") != rpc_id:
            raise DeepSeekApiError("DeepSeek Harness returned an invalid RPC response")
        result = value.get("result")
        if not isinstance(result, dict):
            raise DeepSeekApiError("DeepSeek Harness returned no RPC result")
        if result.get("ok") is not True:
            error = result.get("error")
            if isinstance(error, dict):
                detail = error.get("message") or error.get("code")
            else:
                detail = error
            raise DeepSeekApiError(str(detail or "DeepSeek Harness request failed"))
        response_value = result.get("value")
        if not isinstance(response_value, dict):
            raise DeepSeekApiError("DeepSeek Harness returned an invalid RPC value")
        return response_value


class DeepSeekDriver:
    """Bridge one Drover structured session to one native DSH session."""

    def __init__(
        self,
        command: list[str],
        cwd: str | None,
        emit: EmitFn,
        *,
        native_session_id: str | None = None,
        api: DeepSeekApiClient | None = None,
        poll_interval_s: float = 0.25,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.emit = emit
        self._native_session_id = native_session_id
        self._api = api or DeepSeekApiClient()
        self._poll_interval_s = poll_interval_s
        self._last_native_seq = -1
        self._tool_names: dict[str, str] = {}
        self._lock = threading.Lock()
        self._turn_active = False
        self._turn_thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> None:
        # DSH derives the session's writable root from this cwd (see
        # structured/workspace.py), and the anchor is fixed for the session's
        # whole life -- so an unusable root is refused BEFORE session.create,
        # not reported by a session that already exists with the wrong root.
        workspace = (
            require_workspace_root(self.cwd, harness="DeepSeek Harness")
            if self.cwd
            else None
        )
        if self._native_session_id:
            history = self._history(max_messages=1)
        else:
            payload = {"cwd": workspace} if workspace else {}
            created = self._api.call("session.create", payload)
            native_id = created.get("sessionId")
            if not isinstance(native_id, str) or not native_id:
                raise DeepSeekApiError("DeepSeek Harness did not return a session ID")
            self._native_session_id = native_id
            history = self._history(max_messages=1)
        self._advance_cursor(history)
        if workspace is not None:
            # After the session exists: the git probe is for the transcript,
            # not for the decision, so it must not sit in front of the launch.
            for message in workspace_messages(
                probe_workspace(workspace),
                harness="DeepSeek Harness",
                payload={"native_session_id": self._native_session_id},
            ):
                self._emit(message)
        self._emit(
            StructuredMessage(
                type="status",
                role="system",
                text="ready",
                payload={
                    "awaiting": "input",
                    "native_session_id": self._native_session_id,
                },
            )
        )

    def is_alive(self) -> bool:
        return not self._closed

    def send_turn(
        self,
        text: str,
        turn_id: str,
        images: list | None = None,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("driver is closed")
            if self._turn_active:
                raise RuntimeError("turn already in flight")
            self._turn_active = True
        try:
            if model is not None or thinking_effort is not None:
                self._select_model(model, thinking_effort)
            content: list[dict[str, Any]] = []
            if text:
                content.append({"type": "text", "text": text})
            for image in images or []:
                data = image.get("data_b64")
                media_type = image.get("media_type")
                if isinstance(data, str) and isinstance(media_type, str):
                    content.append(
                        {
                            "type": "image",
                            "mediaType": media_type,
                            "data": data,
                            "name": os.path.basename(str(image.get("path") or "image")),
                        }
                    )
            self._fence_turn()
            self._api.call(
                "session.prompt",
                {
                    "sessionId": self._session_id(),
                    "mode": "queue",
                    "content": content,
                },
            )
        except Exception:
            with self._lock:
                self._turn_active = False
            raise
        worker = threading.Thread(target=self._poll_turn, args=(turn_id,), daemon=True)
        with self._lock:
            self._turn_thread = worker
        worker.start()

    def interrupt(self) -> None:
        with self._lock:
            active = self._turn_active
        if not active:
            return
        try:
            self._api.call("session.cancel", {"sessionId": self._session_id()})
        except DeepSeekApiError:
            # Interrupt has to stay non-throwing, like every other driver's:
            # daemon._interrupt_session only handles KeyError, so an exception
            # raised here escapes the HTTP handler whenever the Web RPC
            # service is down or slow instead of returning an error response.
            pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            active = self._turn_active
            worker = self._turn_thread
        if active:
            try:
                self._api.call("session.cancel", {"sessionId": self._session_id()})
            except DeepSeekApiError:
                pass
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=5)

    def answer_permission(
        self, request_id: str, decision: str, note: str | None = None
    ) -> None:
        del request_id, decision, note
        raise RuntimeError("DeepSeek Harness has no Drover approval channel")

    def _select_model(self, model: str | None, effort: str | None) -> None:
        if model is None:
            current = self._api.call(
                "session.models", {"sessionId": self._session_id()}
            ).get("current")
            if not isinstance(current, dict):
                raise DeepSeekApiError("DeepSeek Harness session has no current model")
            provider = current.get("provider")
            native_model = current.get("model")
        else:
            provider, separator, native_model = model.partition("/")
            if not separator:
                raise DeepSeekApiError(
                    "DeepSeek model IDs must include their provider (provider/model)"
                )
        if not isinstance(provider, str) or not isinstance(native_model, str):
            raise DeepSeekApiError(
                "DeepSeek Harness returned an invalid model selection"
            )
        payload: dict[str, Any] = {
            "sessionId": self._session_id(),
            "provider": provider,
            "model": native_model,
        }
        if effort is not None:
            payload["reasoningEffort"] = effort
        self._api.call("session.selectModel", payload)

    def _poll_turn(self, turn_id: str) -> None:
        failures = 0
        try:
            while True:
                with self._lock:
                    if self._closed:
                        return
                try:
                    items, dropped_before = self._drain_history()
                    failures = 0
                except DeepSeekApiError as exc:
                    failures += 1
                    if failures < 5:
                        time.sleep(self._poll_interval_s)
                        continue
                    self._emit(
                        StructuredMessage(
                            type="status",
                            role="system",
                            text=str(exc),
                            turn_id=turn_id,
                            payload={
                                "turn_complete": True,
                                "awaiting": "input",
                                "error": True,
                                "native_session_id": self._session_id(),
                            },
                        )
                    )
                    return
                if dropped_before is not None:
                    self._emit(
                        StructuredMessage(
                            type="status",
                            role="system",
                            text="native history ran ahead of the poll window",
                            turn_id=turn_id,
                            payload={
                                "history_truncated": True,
                                "dropped_before_seq": dropped_before,
                                "native_session_id": self._session_id(),
                            },
                        )
                    )
                completed = False
                for item in items:
                    event = item.get("event")
                    seq = _event_seq(item)
                    if not isinstance(event, dict) or seq is None:
                        continue
                    if seq <= self._last_native_seq:
                        continue
                    self._last_native_seq = seq
                    for message in self._messages_for(event, item, turn_id):
                        self._emit(message)
                        if (
                            message.type == "status"
                            and message.payload
                            and message.payload.get("turn_complete")
                        ):
                            completed = True
                if completed:
                    return
                time.sleep(self._poll_interval_s)
        finally:
            with self._lock:
                self._turn_active = False
                self._turn_thread = None

    def _emit(self, message: StructuredMessage) -> None:
        """Forward one event unless the session has already been closed.

        ``close()`` can only join the poll thread for a bounded time, while
        that thread may be parked inside a request for as long as the client
        timeout allows, so no join is provably long enough. Making the sink
        inert is the invariant that holds wherever the orphan wakes up: it can
        no longer append events, or flip a finalized session back to
        ``awaiting="input"``, after the manager has dropped the entry.
        """
        with self._lock:
            if self._closed:
                return
        self.emit(message)

    def _messages_for(
        self, event: dict[str, Any], item: dict[str, Any], turn_id: str
    ) -> list[StructuredMessage]:
        event_type = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        native = self._session_id()
        if event_type == "assistant/message" and event.get("surfaceOp") == "append":
            message = data.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            result: list[StructuredMessage] = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") not in {
                    "text",
                    "reasoning",
                }:
                    continue
                result.append(
                    StructuredMessage(
                        type="assistant_output",
                        role="assistant",
                        text=str(block.get("text") or ""),
                        turn_id=turn_id,
                        payload={
                            "thinking": block.get("type") == "reasoning",
                            "usage": data.get("usage"),
                            "native_session_id": native,
                        },
                    )
                )
            return result
        if event_type == "tool/call":
            call_id = str(data.get("callId") or "")
            name = str(data.get("name") or "tool")
            if call_id:
                self._tool_names[call_id] = name
            arguments: Any = data.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    pass
            return [
                StructuredMessage(
                    type="tool_action",
                    role="assistant",
                    text=name,
                    turn_id=turn_id,
                    payload={
                        "tool": name,
                        "tool_use_id": call_id,
                        "input": arguments,
                        "view": item.get("view"),
                        "native_session_id": native,
                    },
                )
            ]
        if event_type == "tool/result":
            message = data.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            results: list[StructuredMessage] = []
            # One result message can carry several tool-result blocks (parallel
            # tool calls batched together), and they are not necessarily first:
            # a leading text block is legal. Surface every one of them.
            for block in blocks if isinstance(blocks, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool-result" and not block.get("toolCallId"):
                    continue
                call_id = str(block.get("toolCallId") or "")
                results.append(
                    StructuredMessage(
                        type="tool_result",
                        role="tool",
                        text=_tool_result_text(block),
                        turn_id=turn_id,
                        payload={
                            "tool": self._tool_names.get(call_id),
                            "tool_use_id": call_id,
                            "is_error": bool(block.get("isError")),
                            "view": item.get("view"),
                            "native_session_id": native,
                        },
                    )
                )
            return results
        if event_type == "turn/end":
            reason = data.get("reason")
            kind = reason.get("kind") if isinstance(reason, dict) else "completed"
            return [
                StructuredMessage(
                    type="status",
                    role="system",
                    text=f"turn {kind}",
                    turn_id=turn_id,
                    payload={
                        "turn_complete": True,
                        "awaiting": "input",
                        "reason": reason,
                        "native_session_id": native,
                    },
                )
            ]
        return []

    def _history(self, *, max_messages: int) -> dict[str, Any]:
        return self._api.call(
            "session.history",
            {"sessionId": self._session_id(), "maxMessages": max_messages},
        )

    def _drain_history(self) -> tuple[list[dict[str, Any]], int | None]:
        """Read history back far enough to reach what has already been emitted.

        Returns the newest window plus the sequence number of the oldest event
        that had to be abandoned, if the bound was hit before the window
        reached the cursor -- a caller-visible truncation rather than a silent
        hole in the transcript.
        """
        window = _HISTORY_TAIL_MESSAGES
        while True:
            history = self._history(max_messages=window)
            raw = history.get("events")
            items = [
                item
                for item in (raw if isinstance(raw, list) else [])
                if isinstance(item, dict)
            ]
            seqs = [seq for seq in map(_event_seq, items) if seq is not None]
            oldest = min(seqs) if seqs else None
            # The window overlaps what the cursor already consumed, so nothing
            # between them can have been missed.
            contiguous = oldest is None or oldest <= self._last_native_seq
            has_more = bool(history.get("hasMore"))
            if contiguous or not has_more:
                return items, None
            if window >= _HISTORY_MAX_MESSAGES:
                return items, oldest
            window = min(window * _HISTORY_WINDOW_GROWTH, _HISTORY_MAX_MESSAGES)

    def _fence_turn(self) -> None:
        """Anchor the cursor to the history that predates this turn's prompt.

        Nothing in the history ties an event to the turn that produced it (see
        the module docstring), so "newer than the cursor" is the only available
        correlation -- and it is only sound if the cursor sits at the end of
        the previous turn when the prompt goes in. A turn that completed while
        no poller was running (harnessd restart plus ``/recover``) would
        otherwise be replayed under the new turn's ID, and its ``turn/end``
        would complete the new turn before it had produced anything.
        """
        self._advance_cursor(self._history(max_messages=1))

    def _advance_cursor(self, history: dict[str, Any]) -> None:
        for item in history.get("events") or []:
            seq = _event_seq(item) if isinstance(item, dict) else None
            if seq is not None:
                self._last_native_seq = max(self._last_native_seq, seq)

    def _session_id(self) -> str:
        if not self._native_session_id:
            raise DeepSeekApiError("DeepSeek Harness session has not started")
        return self._native_session_id


def _event_seq(item: dict[str, Any]) -> int | None:
    event = item.get("event")
    if not isinstance(event, dict):
        return None
    seq = event.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    return seq


def _tool_result_text(block: Any) -> str:
    if not isinstance(block, dict):
        return ""
    content = block.get("content")
    if not isinstance(content, list):
        return str(content or "")
    text: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text.append(str(item.get("text") or ""))
    return "\n".join(text)


def version(command: list[str] | tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            [command[0], "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        raise DeepSeekApiError(f"unable to read dsh version: {exc}") from exc
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise DeepSeekApiError("unable to read dsh version")
    return value
