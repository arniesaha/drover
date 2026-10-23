"""Pure continuity decisions and bounded shadow parity comparison.

The hub deliberately only decides here.  Provider writes remain in the legacy
reconciler until the continuity migration gives the hub its own identity.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Literal

STAGES = ("pr", "merge", "rollout", "canary")
_ACTIONS = frozenset({"none", "nudge", "stall", "handoff", "auto_approve", "wake"})


@dataclass(frozen=True)
class ContinuityPolicy:
    """Deadline policy expressed in minutes; conservative legacy defaults."""

    stall_minutes: float = 60.0
    working_minutes: float = 45.0
    idle_undeclared_minutes: float = 10.0
    ci_pending_minutes: float = 40.0
    release_running_minutes: float = 60.0


@dataclass(frozen=True)
class Decision:
    action: Literal["none", "nudge", "stall", "handoff", "auto_approve", "wake"]
    reason: str | None = None
    issue: int | None = None
    stage: str | None = None
    fingerprint: str = ""

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stage(labels: list[str]) -> str | None:
    reached = [stage for stage in STAGES if f"autonomy:through-{stage}" in labels]
    return reached[-1] if reached else None


def _reached(facts: dict[str, Any], stage: str) -> bool:
    prs = facts.get("prs") or []
    merged = any((pr.get("state") or pr.get("pr_state")) == "MERGED" for pr in prs)
    if stage == "pr":
        return merged or any(
            (pr.get("state") or pr.get("pr_state")) == "OPEN"
            and not pr.get("draft", False)
            and pr.get("checks") == "pass"
            for pr in prs
        )
    if stage == "merge":
        return merged
    markers = facts.get("markers") or {}
    done = set(markers.get("done") or facts.get("done") or [])
    return stage in done or (stage == "rollout" and "canary" in done)


def _marker_seen(facts: dict[str, Any], marker: str, after: datetime) -> bool:
    for bot in facts.get("bot") or []:
        if (
            bot.get("mark") == marker
            and (_time(bot.get("at")) or datetime.min.replace(tzinfo=timezone.utc))
            > after
        ):
            return True
    return False


def _activity(facts: dict[str, Any]) -> datetime | None:
    stamps = [_time(facts.get("created"))]
    stamps.extend(_time(value) for value in facts.get("human_activity") or [])
    stamps.extend(_time(pr.get("last")) for pr in facts.get("prs") or [])
    release = facts.get("release") or {}
    stamps.append(_time(release.get("publishedAt")))
    worker = facts.get("worker") or {}
    stamps.append(_time(worker.get("last_activity")))
    return max((stamp for stamp in stamps if stamp is not None), default=None)


def _deadline(
    facts: dict[str, Any], policy: ContinuityPolicy
) -> tuple[datetime | None, str | None]:
    worker = facts.get("worker") or {}
    state = worker.get("state") or facts.get("worker_state")
    state_at = _time(worker.get("state_at") or facts.get("worker_state_at"))
    if state == "needs_approval":
        return state_at or _time(worker.get("last_activity")), "needs_approval"
    budgets = {
        "working": policy.working_minutes,
        "idle_undeclared": policy.idle_undeclared_minutes,
    }
    if state in budgets and state_at:
        return state_at + timedelta(minutes=budgets[state]), state
    if any(pr.get("checks") == "pending" for pr in facts.get("prs") or []):
        last = max(
            (
                stamp
                for stamp in (_time(pr.get("last")) for pr in facts.get("prs") or [])
                if stamp
            ),
            default=None,
        )
        if last:
            return last + timedelta(minutes=policy.ci_pending_minutes), "ci_pending"
    if (facts.get("release") or {}).get("running"):
        at = _time((facts.get("release") or {}).get("startedAt"))
        if at:
            return (
                at + timedelta(minutes=policy.release_running_minutes),
                "release_running",
            )
    return None, None


def _fingerprint(facts: dict[str, Any], action: str, reason: str | None) -> str:
    markers = facts.get("markers") or {}
    material = {
        "issue": facts.get("number") or facts.get("issue"),
        "action": action,
        "reason": reason,
        "stage": _stage(list(facts.get("labels") or [])),
        "session": markers.get("session") or facts.get("session"),
        "worker_state": (facts.get("worker") or {}).get("state")
        or facts.get("worker_state"),
        "prs": [
            (pr.get("pr"), pr.get("state") or pr.get("pr_state"), pr.get("checks"))
            for pr in facts.get("prs") or []
        ],
    }
    return sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _decision(
    facts: dict[str, Any], action: str, reason: str | None, stage: str | None
) -> Decision:
    return Decision(
        action,
        reason,
        facts.get("number") or facts.get("issue"),
        stage,
        _fingerprint(facts, action, reason),
    )


def decide(
    facts: dict[str, Any],
    now: datetime,
    policy: ContinuityPolicy | dict[str, Any] | None = None,
) -> Decision:
    """Choose at most one continuity action without reading or writing providers."""
    if not isinstance(facts, dict):
        raise ValueError("facts must be an object")
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if policy is None:
        policy = ContinuityPolicy()
    elif isinstance(policy, dict):
        policy = ContinuityPolicy(**policy)
    labels = list(facts.get("labels") or [])
    stage = _stage(labels)
    if not stage:
        return _decision(facts, "none", "no_operator_approved_autonomy_label", stage)
    if "blocked:human" in labels:
        return _decision(facts, "none", "blocked_human", stage)
    if "stalled" in labels:
        return _decision(facts, "none", "stalled", stage)
    if _reached(facts, stage):
        last = _activity(facts) or now
        if not _marker_seen(facts, "handoff", last):
            return _decision(facts, "handoff", None, stage)
        return _decision(facts, "none", "handoff_recorded", stage)

    worker = facts.get("worker") or {}
    if worker.get("state") == "needs_approval":
        gate = worker.get("gate")
        if gate in STAGES and STAGES.index(gate) <= STAGES.index(stage):
            return _decision(facts, "auto_approve", gate, stage)
        return _decision(facts, "wake", "needs_approval", stage)
    if worker.get("state") == "blocked":
        return _decision(facts, "wake", "worker_blocked", stage)

    active = _activity(facts)
    if active is None:
        return _decision(facts, "none", "missing_activity", stage)
    deadline, deadline_kind = _deadline(facts, policy)
    due = deadline or active + timedelta(minutes=policy.stall_minutes)
    if now < due:
        return _decision(facts, "none", None, stage)
    # Legacy markers are deliberately considered after normal activity. Newer
    # worker state deadlines are level-triggered and retain the same two-step
    # nudge/stall protocol.
    if _marker_seen(facts, "nudge", active):
        return _decision(facts, "stall", deadline_kind, stage)
    return _decision(facts, "nudge", deadline_kind, stage)


def action_identity(decision: Decision) -> tuple[str, str]:
    """The rules-engine-shaped stable identity: rule and synthetic head/fingerprint."""
    return "continuity:" + decision.action, decision.fingerprint


@dataclass
class ShadowCompareService:
    """Bounded in-process parity log. It never performs provider writes."""

    limit: int = 100
    mismatches: list[dict[str, Any]] = field(default_factory=list)

    def compare(
        self,
        facts: dict[str, Any],
        local_decision: dict[str, Any],
        *,
        now: datetime,
        policy: ContinuityPolicy | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(local_decision, dict):
            raise ValueError("local_decision must be an object")
        hub = decide(facts, now, policy)
        local_action = local_decision.get("action") or "none"
        if local_action not in _ACTIONS:
            raise ValueError("local_decision.action is unsupported")
        # Existing reconcile emits only action semantics; optional reason lets
        # newer callers compare wake/approval intent without breaking parity.
        local_reason = local_decision.get("reason")
        matches = local_action == hub.action and (
            local_reason is None or local_reason == hub.reason
        )
        response = {
            "matches": matches,
            "hub_decision": hub.payload(),
            "identity": action_identity(hub),
        }
        if not matches:
            record = {
                "local_decision": local_decision,
                "facts_issue": facts.get("number") or facts.get("issue"),
                **response,
            }
            self.mismatches.append(record)
            del self.mismatches[: -self.limit]
            response["recorded"] = True
        else:
            response["recorded"] = False
        return response
