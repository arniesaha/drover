"""Detect provider accounts from host-local harness capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


@dataclass(frozen=True)
class DetectedProvider:
    provider: str
    account_label: str
    host_id: str
    harnesses: tuple[str, ...]
    plan_label: str | None
    usage_status: Literal["supported", "usage_unavailable"]


_PROVIDER_HARNESSES = (
    ("codex", "openai", "Codex", "supported"),
    ("claude-code", "anthropic", "Claude Code", "supported"),
    ("agy", "google", "Antigravity", "supported"),
)


def detect_provider_accounts(capabilities: Mapping[str, Any]) -> list[DetectedProvider]:
    """Return only enabled, locally available provider harnesses.

    Gemini is an inventory-only record. Its CLI exposes no usage subcommand
    and no quota endpoint: quota reaches it only as metadata on a 429, and the
    quota shown in its footer is computed live per response and never
    persisted. There is nothing to poll. Codex and Claude Code both have a
    real source and report windows.
    """
    host_id = str(capabilities.get("host_id") or "local").strip() or "local"
    available = _available_harnesses(capabilities.get("harnesses"))
    accounts: list[DetectedProvider] = []
    for harness, provider, label, usage_status in _PROVIDER_HARNESSES:
        if harness not in available:
            continue
        accounts.append(
            DetectedProvider(
                provider=provider,
                account_label=label,
                host_id=host_id,
                harnesses=(harness,),
                plan_label=None,
                usage_status=usage_status,
            )
        )
    return accounts


def _available_harnesses(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    available: set[str] = set()
    for item in value:
        if isinstance(item, str):
            available.add(item)
            continue
        if not isinstance(item, Mapping) or item.get("enabled") is False:
            continue
        harness = item.get("id") or item.get("name")
        if isinstance(harness, str) and harness:
            available.add(harness)
    return available
