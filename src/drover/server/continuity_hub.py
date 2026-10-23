"""Authenticated-boundary service for hub-owned continuity decisions.

Clients submit bounded, normalized facts.  The hub decides exactly one action and,
when actionable, commits its durable wake in the same control-plane transaction.
Clients may execute the resulting work locally, but cannot choose a different
continuity action through this API.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from drover.server.continuity_loop import ContinuityPolicy, Decision, decide, normalize_facts
from drover.server.continuity_wakes import ContinuityWakes, _text


class ContinuityHub:
    """Hub-only decision adapter over the durable wake outbox."""

    def __init__(self, path: str | Path, **kwargs: Any):
        self.wakes = ContinuityWakes(path, **kwargs)

    def decide(
        self,
        *,
        issue_key: str,
        facts: dict[str, Any],
        now: datetime | None = None,
        policy: ContinuityPolicy | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _text(issue_key, "issue_key", 191)
        normalized = normalize_facts(facts)
        at = now or datetime.now(timezone.utc)
        decision = decide(normalized, at, policy)
        wake_id = None
        # ``enqueue_decision`` opens one transaction for the identity lookup and
        # insert, so two racing thin clients retain one identical durable wake.
        if decision.action != "none":
            wake_id = self.wakes.enqueue_decision(
                issue_key=issue_key,
                decision=decision,
                facts=normalized,
                decided_at=at,
            )
        return {
            "issue_key": issue_key,
            "facts": normalized,
            "decision": decision.payload(),
            "wake_id": wake_id,
        }

    def lease(self, issue_key: str, **kwargs: Any) -> dict:
        return self.wakes.lease(issue_key, **kwargs)

    def consume(self, issue_key: str, **kwargs: Any) -> dict | None:
        return self.wakes.consume(issue_key, **kwargs)

    def acknowledge(self, issue_key: str, **kwargs: Any) -> None:
        self.wakes.acknowledge(issue_key, **kwargs)

    def status(self, issue_key: str, **kwargs: Any) -> dict:
        return self.wakes.status(issue_key, **kwargs)
