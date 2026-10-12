"""Supported provider routing constraints and public Anthropic usage semantics."""

import logging

log = logging.getLogger(__name__)

# extra_usage is a billing allowance, not a timed subscription constraint.
ANTHROPIC_WINDOW_MINUTES = {
    "five_hour": 300,
    "seven_day": 10080,
    "seven_day_opus": 10080,
    "seven_day_sonnet": 10080,
    "extra_usage": None,
}


_ROUTING_KINDS = {
    "openai": frozenset({"primary", "secondary"}),
    "anthropic": frozenset(
        kind
        for kind, minutes in ANTHROPIC_WINDOW_MINUTES.items()
        if minutes is not None
    ),
    "google": frozenset(
        {
            "five_hour",
            "seven_day",
            "five_hour_claude_gpt",
            "seven_day_claude_gpt",
        }
    ),
}


def is_public_window(provider: str, kind: str) -> bool:
    if provider != "anthropic" or kind in ANTHROPIC_WINDOW_MINUTES:
        return True
    log.debug("Ignoring unsupported Anthropic usage field: %s", kind)
    return False


def is_routing_window(provider: str, kind: str) -> bool:
    return is_public_window(provider, kind) and kind in _ROUTING_KINDS.get(
        provider, frozenset()
    )
