"""Claude Code structured driver (bidirectional stream-json).

Wire shapes below are grounded in ``tests/fixtures/structured/FINDINGS.md``
(Task 0's live probe of ``claude`` 2.1.201), except for the
``control_request``/``control_response`` approval exchange: that shape is
**doc-derived and unverified against a live capture** (see FINDINGS.md
section "1(d)" and "Concerns / follow-ups" item 2 — the probe host's global
``permissions.defaultMode: "auto"`` suppressed every attempt to trigger a
live approval round-trip). Treat the approval prompt/response mapping here
as best-effort until re-probed on a host without that override.
"""

from __future__ import annotations

import json
import os
import shutil
from collections import OrderedDict
from pathlib import Path
from typing import Any

from drover.server.harness.structured.driver import ProcessDriver, StructuredMessage
from drover.server.staging_credentials import claude_command, is_staging


def resolve_binary(binary: str | None = None) -> str | None:
    """Locate the ``claude`` executable the way the daemon does.

    ``None`` means nothing was found: PATH has no ``claude`` and no versioned
    install exists. Callers that can degrade (the summarizer's harness
    backend) need to tell that apart from a CLI that ran and failed.
    """
    return binary or shutil.which("claude") or _versioned_claude_binary()


def default_command(binary: str | None = None) -> list[str]:
    resolved_binary = resolve_binary(binary)
    return claude_command(
        [
            resolved_binary or "claude",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            # M5: structured sessions run headless — without an answered
            # control_request channel, any gated tool call fails outright
            # ("requested permissions ... but you haven't granted it"), so the
            # only workable posture until approval surfacing (Part B) lands is
            # full bypass, matching codex danger-full-access / agy skip-permissions.
            "--permission-mode",
            "bypassPermissions",
        ]
    )


def resume_command(command: list[str], native_session_id: str | None) -> list[str]:
    resumed = list(command)
    if native_session_id:
        resumed.extend(["--resume", native_session_id])
    return resumed


def child_env() -> dict[str, str]:
    """Sanitized environment for a spawned ``claude`` child process.

    FINDINGS.md documents that ambient ``CLAUDECODE``/``CLAUDE_CODE_*`` env
    vars (present when the harness itself runs nested inside a Claude Code
    session) leak into the child and change its behavior even when
    ``--setting-sources ""`` is passed. Strip anything starting with
    ``CLAUDE`` so a spawned driver process sees a clean environment.
    """
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("CLAUDE")
    }
    if is_staging():
        env = {
            key: value for key, value in env.items() if not key.startswith("ANTHROPIC")
        }
    return env


def _versioned_claude_binary(home: Path | None = None) -> str | None:
    versions_dir = (home or Path.home()) / ".local/share/claude/versions"
    if not versions_dir.is_dir():
        return None
    candidates = [
        path
        for path in versions_dir.iterdir()
        if path.is_file() and os.access(path, os.X_OK)
    ]
    candidates.sort(key=lambda path: _version_key(path.name), reverse=True)
    return str(candidates[0]) if candidates else None


def _version_key(version: str) -> tuple[tuple[int, int, str], ...]:
    # Numeric parts sort above non-numeric ones so mixed names (e.g. a
    # "latest" symlink next to "2.1.201") never compare int against str.
    parts: list[tuple[int, int, str]] = []
    for part in version.replace("-", ".").split("."):
        if part.isdigit():
            parts.append((1, int(part), ""))
        else:
            parts.append((0, 0, part))
    return tuple(parts)


class ClaudeDriver(ProcessDriver):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # tool_use_id -> tool name, so a later tool_result can carry the
        # originating tool's name for display (iOS card titles). Unbounded
        # but tiny: one short string pair per tool call in the session.
        self._tool_names: dict[str, str] = {}
        self._stream_message_id: str | None = None
        self._stream_model: str | None = None
        # Claude emits one-block snapshots, whose local index is always zero.
        # Retain recent wire indexes by message for delayed snapshots as well.
        self._stream_blocks: OrderedDict[str, dict[int, str]] = OrderedDict()
        self._finalized_blocks: dict[str, set[int]] = {}

    def parse_line(self, line: str) -> list[StructuredMessage]:
        obj = json.loads(line)
        kind = obj.get("type")
        if kind == "stream_event":
            return self._from_stream_event(obj)
        if kind == "system":
            return [
                StructuredMessage(
                    type="status",
                    role="system",
                    text=str(obj.get("subtype") or "system"),
                    payload={"native_session_id": obj.get("session_id"), **obj},
                )
            ]
        if kind == "assistant":
            return self._from_content(obj, role="assistant")
        if kind == "user":
            return self._from_content(obj, role="tool")
        if kind == "result":
            return [
                StructuredMessage(
                    type="status",
                    role="system",
                    text="turn complete",
                    payload={
                        "turn_complete": True,
                        "awaiting": "input",
                        "result": obj,
                    },
                )
            ]
        if kind == "control_request":
            request = obj.get("request") or {}
            return [
                StructuredMessage(
                    type="approval_prompt",
                    role="system",
                    text=f"approval needed: {request.get('tool_name')}",
                    payload={
                        "request_id": obj.get("request_id"),
                        "tool": request.get("tool_name"),
                        "input": request.get("input"),
                    },
                )
            ]
        # Unknown-but-valid-JSON top-level event kind (e.g. rate_limit_event,
        # or any future event type not enumerated above). This is NOT the
        # same as unparseable output — the base ProcessDriver's stdout pump
        # already degrades non-JSON lines to type="raw" before parse_line is
        # ever reached (see driver.py's _pump_stdout try/except). Real
        # captured Claude output always maps to a typed message here.
        return [
            StructuredMessage(
                type="status",
                role="system",
                text=str(kind),
                payload=obj,
            )
        ]

    def _from_stream_event(self, obj: dict[str, Any]) -> list[StructuredMessage]:
        event = obj.get("event") or {}
        if event.get("type") == "message_start":
            message = event.get("message") or {}
            self._stream_message_id = message.get("id")
            self._stream_model = message.get("model")
            if self._stream_message_id:
                self._stream_blocks.setdefault(self._stream_message_id, {})
                while len(self._stream_blocks) > 32:
                    old_id, _ = self._stream_blocks.popitem(last=False)
                    self._finalized_blocks.pop(old_id, None)
        elif event.get("type") == "content_block_start":
            self._remember_block(event, (event.get("content_block") or {}).get("type"))
        elif event.get("type") == "content_block_delta":
            delta = event.get("delta") or {}
            thinking = delta.get("type") == "thinking_delta"
            if delta.get("type") == "text_delta" or thinking:
                self._remember_block(event, "thinking" if thinking else "text")
                text = delta.get("thinking" if thinking else "text") or ""
                if text:
                    payload = _metadata_payload(obj, {})
                    payload["model"] = self._stream_model
                    payload["text_stream_delta"] = True
                    if self._stream_message_id:
                        payload["text_stream_id"] = (
                            f"{self._stream_message_id}:{event['index']}"
                        )
                    if thinking:
                        payload["thinking"] = True
                    return [
                        StructuredMessage(
                            type="assistant_output",
                            role="assistant",
                            text=text,
                            payload=payload,
                        )
                    ]
        elif event.get("type") == "message_delta" and event.get("usage"):
            return [
                StructuredMessage(
                    type="status",
                    role="system",
                    text="usage",
                    payload=_metadata_payload(
                        obj,
                        {
                            "id": self._stream_message_id,
                            "model": self._stream_model,
                            "usage": event["usage"],
                        },
                    ),
                )
            ]
        # Tool JSON is handled by the complete assistant snapshot. Lifecycle
        # envelopes have no transcript text and must not flood the app feed.
        return []

    def _remember_block(self, event: dict[str, Any], block_type: str | None) -> None:
        if (
            self._stream_message_id
            and block_type
            and isinstance(event.get("index"), int)
        ):
            self._stream_blocks[self._stream_message_id][event["index"]] = block_type

    def _snapshot_index(
        self, message_id: str, block_type: str, index: int, count: int
    ) -> int:
        blocks = self._stream_blocks.get(message_id, {})
        finalized = (
            self._finalized_blocks.setdefault(message_id, set()) if blocks else set()
        )
        if count == 1:
            candidates = [
                i
                for i, kind in blocks.items()
                if kind == block_type and i not in finalized
            ]
            if candidates:
                index = min(candidates)
            elif (
                len(matches := [i for i, kind in blocks.items() if kind == block_type])
                == 1
            ):
                index = matches[0]
        finalized.add(index)
        return index

    def _from_content(
        self, obj: dict[str, Any], *, role: str
    ) -> list[StructuredMessage]:
        messages: list[StructuredMessage] = []
        message = obj.get("message") or {}
        base_payload = _metadata_payload(obj, message)
        content = message.get("content") or []
        for index, block in enumerate(content):
            block_type = block.get("type")
            block_payload = dict(base_payload)
            if (
                role == "assistant"
                and message.get("id")
                and block_type in ("text", "thinking")
            ):
                # The snapshot replaces this block's deltas, including usage.
                stream_index = self._snapshot_index(
                    message["id"], block_type, index, len(content)
                )
                block_payload["text_stream_id"] = f"{message['id']}:{stream_index}"
                block_payload["text_stream_final"] = True
            if block_type == "text":
                messages.append(
                    StructuredMessage(
                        type="assistant_output",
                        role="assistant",
                        text=block.get("text") or "",
                        payload=block_payload,
                    )
                )
            elif block_type == "thinking":
                messages.append(
                    StructuredMessage(
                        type="assistant_output",
                        role="assistant",
                        text=block.get("thinking") or "",
                        payload={**block_payload, "thinking": True},
                    )
                )
            elif block_type == "tool_use":
                if block.get("id") and block.get("name"):
                    self._tool_names[str(block["id"])] = str(block["name"])
                messages.append(
                    StructuredMessage(
                        type="tool_action",
                        role="assistant",
                        text=f"{block.get('name')}",
                        payload={
                            **base_payload,
                            "tool": block.get("name"),
                            "tool_use_id": block.get("id"),
                            "input": block.get("input"),
                        },
                    )
                )
            elif block_type == "tool_result":
                messages.append(
                    StructuredMessage(
                        type="tool_result",
                        role="tool",
                        text=_result_text(block),
                        payload={
                            **base_payload,
                            "tool_use_id": block.get("tool_use_id"),
                            "tool": self._tool_names.get(str(block.get("tool_use_id"))),
                            "is_error": block.get("is_error"),
                        },
                    )
                )
        # Empty content array (or a content array containing only block
        # types we don't recognize) never falls back to type="raw" — that
        # sentinel is reserved for lines that failed to parse as JSON at
        # all. A real, valid event with nothing to say still becomes a
        # typed status message.
        return messages or [
            StructuredMessage(
                type="status",
                role=role,
                text="empty content",
                payload={**base_payload, "empty_content": True, "event": obj},
            )
        ]

    def send_turn(
        self,
        text: str,
        turn_id: str,
        images: list | None = None,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> None:
        # The Claude stream is persistent; model/effort are startup flags.
        del turn_id, model, thinking_effort
        content: list[dict] = [{"type": "text", "text": text}]
        # Attachments also appear as [Attached image: <path>] lines in the
        # text (that's the only channel codex/agy have); the base64 block
        # here lets Claude see the image without a Read tool call.
        for image in images or []:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image["media_type"],
                        "data": image["data_b64"],
                    },
                }
            )
        self.send_line(
            {
                "type": "user",
                "message": {"role": "user", "content": content},
            }
        )

    def answer_permission(
        self, request_id: str, decision: str, note: str | None = None
    ) -> None:
        behavior = "allow" if decision == "allow" else "deny"
        self.send_line(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {"behavior": behavior, "message": note or ""},
                },
            }
        )


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
    return ""


def _metadata_payload(event: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if message.get("usage") is not None:
        payload["usage"] = message.get("usage")
        if message.get("id"):
            payload["usage_message_id"] = message["id"]
    if message.get("model") is not None:
        payload["model"] = message.get("model")
    if event.get("session_id") is not None:
        payload["native_session_id"] = event.get("session_id")
    if event.get("uuid") is not None:
        payload["native_event_id"] = event.get("uuid")
    return payload
