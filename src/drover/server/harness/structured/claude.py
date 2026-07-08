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
from typing import Any

from drover.server.harness.structured.driver import ProcessDriver, StructuredMessage


def default_command(binary: str | None = None) -> list[str]:
    return [
        binary or shutil.which("claude") or "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def child_env() -> dict[str, str]:
    """Sanitized environment for a spawned ``claude`` child process.

    FINDINGS.md documents that ambient ``CLAUDECODE``/``CLAUDE_CODE_*`` env
    vars (present when the harness itself runs nested inside a Claude Code
    session) leak into the child and change its behavior even when
    ``--setting-sources ""`` is passed. Strip anything starting with
    ``CLAUDE`` so a spawned driver process sees a clean environment.
    """
    return {
        key: value for key, value in os.environ.items() if not key.startswith("CLAUDE")
    }


class ClaudeDriver(ProcessDriver):
    def parse_line(self, line: str) -> list[StructuredMessage]:
        obj = json.loads(line)
        kind = obj.get("type")
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

    def _from_content(
        self, obj: dict[str, Any], *, role: str
    ) -> list[StructuredMessage]:
        messages: list[StructuredMessage] = []
        content = (obj.get("message") or {}).get("content") or []
        for block in content:
            block_type = block.get("type")
            if block_type == "text":
                messages.append(
                    StructuredMessage(
                        type="assistant_output",
                        role="assistant",
                        text=block.get("text") or "",
                    )
                )
            elif block_type == "thinking":
                messages.append(
                    StructuredMessage(
                        type="assistant_output",
                        role="assistant",
                        text=block.get("thinking") or "",
                        payload={"thinking": True},
                    )
                )
            elif block_type == "tool_use":
                messages.append(
                    StructuredMessage(
                        type="tool_action",
                        role="assistant",
                        text=f"{block.get('name')}",
                        payload={
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
                        payload={"tool_use_id": block.get("tool_use_id")},
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
                payload={"empty_content": True, "event": obj},
            )
        ]

    def send_turn(self, text: str, turn_id: str) -> None:
        del turn_id  # not part of Claude's wire shape; caller-side bookkeeping only
        self.send_line(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
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
