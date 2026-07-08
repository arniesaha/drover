"""Context-container vocabulary for broad Drover handoff context.

Context containers are intentionally separate from repository attribution: a
container may have repo evidence, but non-code conversations and explicit general
activity remain first-class, resumable context instead of attribution failures.
"""

from __future__ import annotations

CONTEXT_CONTAINER_TYPES = frozenset(
    {
        "code_project",
        "operational_project",
        "personal_project",
        "research_thread",
        "open_floor_conversation",
        "general_activity",
    }
)

DEFAULT_CONTEXT_REDACTION_POLICY = "session-summary-redacted"


def normalize_context_type(value: str) -> str:
    """Return a supported context container type or raise ValueError."""
    normalized = str(value or "").strip()
    if normalized not in CONTEXT_CONTAINER_TYPES:
        allowed = ", ".join(sorted(CONTEXT_CONTAINER_TYPES))
        raise ValueError(
            f"unsupported context container type {value!r}; expected one of: {allowed}"
        )
    return normalized
