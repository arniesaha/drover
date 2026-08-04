"""Normalized Drover session event helpers for Meta Harness."""

from __future__ import annotations

import re
from typing import Any

NORMALIZED_EVENT_TYPES = {
    "assistant_output",
    "user_input",
    "tool_action",
    "tool_result",
    "approval_prompt",
    "approval_response",
    "file_change",
    "command",
    "error",
    "status",
    "handoff_marker",
}

_STATUS_EVENTS = {
    "session.started",
    "session.exited",
    "session.terminated",
    "terminal.attached",
    "terminal.detached",
    "terminal.resized",
    "terminal.interrupt",
}

_HANDOFF_EVENTS = {
    "session.continued",
    "session.handoff",
    "terminal.initial_input",
}

_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"
    r"|\][^\x07]*(?:\x07|\x1b\\)"
    r"|[PX^_].*?\x1b\\"
    r"|[@-_]"
    r")"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def normalize_harness_event(
    *,
    event_type: str,
    payload: dict[str, Any] | None = None,
    harness: str | None = None,
    normalized_type: str | None = None,
    normalized_source: str | None = None,
    content_preview: str | None = None,
) -> dict[str, str]:
    """Return stable mobile/session UI metadata for a harness event.

    Sources are intentionally explicit: ``structured`` means Drover or a provider
    hook supplied the semantic event, while ``inferred_terminal`` means Drover
    inferred it from PTY text or controls.
    """

    payload = payload or {}
    kind = _clean_type(normalized_type) or _infer_type(event_type, payload, harness)
    source = normalized_source or _infer_source(event_type, payload)
    preview = _bounded_preview(
        content_preview
        if content_preview is not None
        else _infer_preview(event_type, payload, kind)
    )
    return {
        "normalized_type": kind,
        "normalized_source": source,
        "content_preview": preview,
    }


def _clean_type(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return text if text in NORMALIZED_EVENT_TYPES else None


def _infer_type(event_type: str, payload: dict[str, Any], harness: str | None) -> str:
    # Structured drivers emit the semantic name directly as the event type
    # ("assistant_output", "tool_action", "tool_result", "user_input",
    # "error"). None of the prefix rules below match those bare underscore
    # names -- they only match dotted terminal./session./tool. types -- so
    # without this pass-through every structured event fell through to the
    # "status" default, collapsing the whole transcript into one bucket.
    if event_type in NORMALIZED_EVENT_TYPES:
        return event_type
    if event_type in {"terminal.input", "terminal.initial_input"}:
        data = str(payload.get("text") or "")
        if harness == "shell" and _looks_like_command(data):
            return "command"
        return "user_input"
    if event_type in {"terminal.output", "provider.output"}:
        text = str(payload.get("text") or payload.get("content") or "")
        lowered = text.lower()
        if "approve" in lowered and "deny" in lowered:
            return "approval_prompt"
        if "error" in lowered or "traceback" in lowered:
            return "error"
        if _looks_like_tool_output(text):
            return "tool_action"
        if _looks_like_file_change(text):
            return "file_change"
        return "assistant_output"
    if event_type.startswith("error.") or event_type.endswith(".error"):
        return "error"
    if event_type in _HANDOFF_EVENTS:
        return "handoff_marker"
    if event_type in _STATUS_EVENTS or event_type.startswith("session."):
        return "status"
    if event_type.startswith("tool."):
        return "tool_action"
    if event_type.startswith("approval."):
        return "approval_prompt"
    if event_type.startswith("file."):
        return "file_change"
    return "status"


def _infer_source(event_type: str, payload: dict[str, Any]) -> str:
    explicit = payload.get("normalized_source") or payload.get("source")
    if explicit in {"structured", "inferred_terminal", "nexus_control"}:
        return str(explicit)
    if event_type.startswith("terminal."):
        if event_type in {
            "terminal.output",
            "terminal.input",
            "terminal.initial_input",
        }:
            return "inferred_terminal"
        return "nexus_control"
    if event_type.startswith(("session.", "tool.", "approval.", "file.")):
        return "structured"
    return "structured"


def _infer_preview(event_type: str, payload: dict[str, Any], kind: str) -> str:
    for key in ("text", "content", "summary", "message", "error", "command"):
        value = payload.get(key)
        if value:
            return str(value)
    if kind == "status":
        return event_type.replace(".", " ")
    if kind == "handoff_marker":
        return "handoff marker"
    return event_type


def _bounded_preview(value: str | None, *, limit: int = 2000) -> str:
    text = clean_terminal_preview(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def clean_terminal_preview(value: str) -> str:
    """Remove terminal repaint/control bytes while preserving readable text."""

    text = _ANSI_RE.sub("", str(value)).replace("\r", "")
    text = _CONTROL_RE.sub("", text)
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip())


def _looks_like_command(text: str) -> bool:
    stripped = text.strip()
    if not stripped or "\n" not in text:
        return False
    first = stripped.splitlines()[0]
    return bool(first and not first.startswith(("\x1b", "\t")))


def _looks_like_tool_output(text: str) -> bool:
    lowered = text.lower()
    markers = ("tool", "running", "executing", "read(", "write(", "bash", "pytest")
    return any(marker in lowered for marker in markers)


def _looks_like_file_change(text: str) -> bool:
    lowered = text.lower()
    markers = ("modified", "created", "deleted", "diff --git", "apply_patch")
    return any(marker in lowered for marker in markers)
