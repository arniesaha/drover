"""Build and normalize bounded prompts for live session recaps."""

from __future__ import annotations

import re
from importlib.resources import files
from typing import Any, Iterable

from drover.server.harness.auth import redact_auth_text

_CONTENT_EVENT_TYPES = {
    "user_input",
    "assistant_output",
    "tool_action",
    "tool_result",
}
_MAX_EVENTS = 30
_MAX_EVENT_CHARS = 500
_MARKDOWN_RE = re.compile(r"(?:`{1,3}|\*{1,3}|^#{1,6}\s*)", re.MULTILINE)
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")
_WHITESPACE_RE = re.compile(r"\s+")
# The sentence subject, and the auxiliary that would be left dangling without
# it. Models narrate recaps as "The user is doing X"; on a two-line inbox card
# those words cost a third of the first line and say nothing that tells one
# session apart from another. Anchored, and requiring whitespace after "user",
# so a possessive ("the user's credentials") or a mid-sentence mention
# ("adding a user lookup") is left alone -- there the word is the topic.
_USER_SUBJECT_RE = re.compile(
    r"\A(?:the\s+)?user\s+(?:(?:is|are|was|were|has|have|had)(?:\s+|\Z))?",
    re.IGNORECASE,
)


def load_live_recap_template() -> str:
    """Return the versioned live-recap prompt template."""
    return (files("drover") / "prompts" / "live_session_recap.md").read_text()


def build_live_recap_prompt(
    events: Iterable[dict[str, Any]], *, template: str | None = None
) -> str:
    """Build a prompt from the newest bounded, content-bearing event previews."""
    content_events = [
        event
        for event in events
        if event.get("event_type") in _CONTENT_EVENT_TYPES
        and isinstance(event.get("content_preview"), str)
        and event["content_preview"]
    ]
    if all(isinstance(event.get("seq"), int) for event in content_events):
        content_events.sort(key=lambda event: event["seq"])

    # Redact before clipping so a secret that crosses the cap cannot leak as
    # an unrecognizable prefix. Stored previews from structured drivers can be
    # inferred directly from event text.
    previews = [
        redact_auth_text(event["content_preview"])[:_MAX_EVENT_CHARS]
        for event in content_events[-_MAX_EVENTS:]
    ]
    turns = "\n\n---\n\n".join(previews) or "(no events)"
    tmpl = template if template is not None else load_live_recap_template()
    return tmpl.replace("{turns}", turns)


def drop_user_subject(text: str) -> str:
    """Strip a leading "The user is ..." and recapitalize what is left.

    Returns an empty string when the subject was the whole sentence, since a
    recap of "Is" is worse than no recap at all.
    """
    remainder = _USER_SUBJECT_RE.sub("", text, count=1).lstrip()
    if remainder == text:
        return text
    if not remainder:
        return ""
    # Only the first character: the rest may carry meaningful case ("OTLP").
    return remainder[0].upper() + remainder[1:]


def normalize_live_recap(value: Any, *, max_chars: int = 160) -> str:
    """Return one bounded plain-text recap, or an empty string for invalid input."""
    if not isinstance(value, str) or max_chars <= 0:
        return ""

    text = _WHITESPACE_RE.sub(" ", _MARKDOWN_RE.sub("", value)).strip()
    # Before the length cap, so the budget is spent on the recap and not on
    # its subject.
    text = drop_user_subject(text)
    sentence_end = _SENTENCE_END_RE.search(text)
    if sentence_end is not None:
        text = text[: sentence_end.end()]
    if len(text) <= max_chars:
        return text

    bounded = text[:max_chars]
    boundary = bounded.rfind(" ")
    return bounded[:boundary].rstrip() if boundary > 0 else ""
