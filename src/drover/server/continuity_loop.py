"""Pure continuity decisions and bounded shadow parity comparison.

The hub deliberately only decides here.  Provider writes remain in the legacy
reconciler until the continuity migration gives the hub its own identity.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Literal

STAGES = ("pr", "merge", "rollout", "canary")
_ACTIONS = frozenset({"none", "nudge", "stall", "handoff", "auto_approve", "wake"})

# These are the comments written by the still-authoritative GitHub reconciler.
# Keep the wire values here, rather than its convenient action names, because
# shadow facts can be projected directly from older reconcile runs.
NUDGE_MARK = "<!-- drover-reconcile:nudge -->"
STALL_MARK = "<!-- drover-reconcile:stalled -->"
HANDOFF_MARK = "<!-- drover-reconcile:handoff -->"
_MARK_ACTIONS = {
    NUDGE_MARK: "nudge",
    STALL_MARK: "stall",
    HANDOFF_MARK: "handoff",
    # Compatibility with the first shadow projection, which translated these
    # wires before posting facts.
    "nudge": "nudge",
    "stall": "stall",
    "handoff": "handoff",
}
_SHADOW_SERVICE_INIT_LOCK = threading.Lock()


@dataclass(frozen=True)
class ContinuityPolicy:
    """Deadline policy expressed in minutes; conservative legacy defaults."""

    stall_minutes: float = 30.0
    working_minutes: float = 45.0
    idle_undeclared_minutes: float = 10.0
    ci_pending_minutes: float = 40.0
    release_running_minutes: float = 60.0
    explicit_deadlines: bool = False


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
            _MARK_ACTIONS.get(bot.get("mark")) == marker
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


def _legacy_touched(facts: dict[str, Any], activity: datetime) -> datetime:
    """The reconciler's idle clock includes its own post-activity markers."""
    markers = [
        _time(bot.get("at"))
        for bot in facts.get("bot") or []
        if _time(bot.get("at")) is not None and _time(bot.get("at")) > activity
    ]
    return max([activity, *markers])


def _bounded_text(value: Any, *, limit: int) -> str | None:
    return value if isinstance(value, str) and len(value) <= limit else None


def normalize_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Validate the small, provider-free fact shape accepted at the HTTP edge."""
    if not isinstance(facts, dict):
        raise ValueError("facts must be an object")
    number = facts.get("number", facts.get("issue"))
    if type(number) is not int or number < 1:
        raise ValueError("facts.number must be a positive integer")
    labels = facts.get("labels", [])
    if (
        not isinstance(labels, list)
        or len(labels) > 64
        or any(_bounded_text(label, limit=191) is None for label in labels)
    ):
        raise ValueError("facts.labels must be at most 64 bounded strings")

    def timestamp(value: Any, name: str, *, required: bool = False) -> str | None:
        if value is None and not required:
            return None
        text = _bounded_text(value, limit=64)
        if text is None or _time(text) is None:
            raise ValueError(f"{name} must be an ISO timestamp")
        return text

    created = timestamp(facts.get("created"), "facts.created")
    human_activity = facts.get("human_activity", [])
    if not isinstance(human_activity, list) or len(human_activity) > 100:
        raise ValueError("facts.human_activity must be a bounded list")
    human_activity = [
        timestamp(value, "facts.human_activity", required=True)
        for value in human_activity
    ]

    prs = facts.get("prs", [])
    if not isinstance(prs, list) or len(prs) > 30:
        raise ValueError("facts.prs must be a bounded list")
    normalized_prs = []
    for pr in prs:
        if not isinstance(pr, dict):
            raise ValueError("facts.prs entries must be objects")
        pr_number = pr.get("pr")
        if type(pr_number) is not int or pr_number < 1:
            raise ValueError("facts.prs entries require a positive pr number")
        state = _bounded_text(pr.get("state") or pr.get("pr_state"), limit=32)
        checks = _bounded_text(pr.get("checks"), limit=32)
        if (
            state is None
            or checks is None
            or not isinstance(pr.get("draft", False), bool)
        ):
            raise ValueError("facts.prs entries are invalid")
        normalized_prs.append(
            {
                "pr": pr_number,
                "state": state,
                "draft": pr.get("draft", False),
                "checks": checks,
                "last": timestamp(pr.get("last"), "facts.prs.last"),
            }
        )

    markers = facts.get("markers", {})
    if not isinstance(markers, dict):
        raise ValueError("facts.markers must be an object")
    done = markers.get("done", facts.get("done", []))
    if (
        not isinstance(done, list)
        or len(done) > 8
        or any(_bounded_text(item, limit=32) is None for item in done)
    ):
        raise ValueError("facts.markers.done is invalid")
    session = _bounded_text(markers.get("session"), limit=191)
    if markers.get("session") is not None and session is None:
        raise ValueError("facts.markers.session is invalid")

    bot = facts.get("bot", [])
    if not isinstance(bot, list) or len(bot) > 100:
        raise ValueError("facts.bot must be a bounded list")
    normalized_bot = []
    for item in bot:
        if not isinstance(item, dict):
            raise ValueError("facts.bot entries must be objects")
        mark = _bounded_text(item.get("mark"), limit=64)
        if mark is None:
            raise ValueError("facts.bot entries require a bounded mark")
        normalized_bot.append(
            {
                "mark": mark,
                "at": timestamp(item.get("at"), "facts.bot.at", required=True),
            }
        )

    worker = facts.get("worker", {})
    if not isinstance(worker, dict):
        raise ValueError("facts.worker must be an object")
    normalized_worker = {}
    for field, limit in (("state", 64), ("gate", 32)):
        value = worker.get(field)
        text = _bounded_text(value, limit=limit)
        if value is not None and text is None:
            raise ValueError(f"facts.worker.{field} is invalid")
        normalized_worker[field] = text
    normalized_worker["state_at"] = timestamp(
        worker.get("state_at"), "facts.worker.state_at"
    )
    normalized_worker["last_activity"] = timestamp(
        worker.get("last_activity"), "facts.worker.last_activity"
    )

    release = facts.get("release", {})
    if not isinstance(release, dict) or not isinstance(
        release.get("running", False), bool
    ):
        raise ValueError("facts.release must be an object with boolean running")
    normalized_release = {
        "publishedAt": timestamp(
            release.get("publishedAt"), "facts.release.publishedAt"
        ),
        "startedAt": timestamp(release.get("startedAt"), "facts.release.startedAt"),
        "running": release.get("running", False),
    }
    return {
        "number": number,
        "labels": labels,
        "created": created,
        "human_activity": human_activity,
        "prs": normalized_prs,
        "markers": {"session": session, "done": done},
        "bot": normalized_bot,
        "worker": normalized_worker,
        "release": normalized_release,
    }


def normalize_policy(
    policy: ContinuityPolicy | dict[str, Any] | None,
) -> ContinuityPolicy:
    if policy is None:
        return ContinuityPolicy()
    if isinstance(policy, ContinuityPolicy):
        return policy
    if not isinstance(policy, dict):
        raise ValueError("policy must be an object")
    allowed = set(ContinuityPolicy.__dataclass_fields__)
    if set(policy) - allowed:
        raise ValueError("policy contains unsupported fields")
    for key, value in policy.items():
        if key == "explicit_deadlines":
            if not isinstance(value, bool):
                raise ValueError("policy.explicit_deadlines must be boolean")
        elif (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("policy values must be finite non-negative minutes")
    return ContinuityPolicy(**policy)


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


def normalize_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded facts projection accepted from thin clients.

    This intentionally drops unknown input rather than letting a client smuggle
    provider commands or unbounded GitHub bodies through the wake outbox.
    """
    if not isinstance(facts, dict):
        raise ValueError("facts must be an object")
    def text(value: Any, limit: int = 512) -> str | None:
        return value if isinstance(value, str) and len(value) <= limit else None
    number = facts.get("number", facts.get("issue"))
    if type(number) is not int or number < 1:
        raise ValueError("facts.number must be a positive integer")
    labels = facts.get("labels") or []
    if not isinstance(labels, list) or len(labels) > 64 or any(not text(v, 191) for v in labels):
        raise ValueError("facts.labels must be at most 64 bounded strings")
    def stamps(value: Any, name: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 100:
            raise ValueError(f"facts.{name} must be a bounded list")
        result = [text(item, 64) for item in value]
        if any(item is None or _time(item) is None for item in result):
            raise ValueError(f"facts.{name} must contain ISO timestamps")
        return result  # type: ignore[return-value]
    prs = facts.get("prs") or []
    if not isinstance(prs, list) or len(prs) > 30:
        raise ValueError("facts.prs must be a bounded list")
    normalized_prs = []
    for pr in prs:
        if not isinstance(pr, dict):
            raise ValueError("facts.prs entries must be objects")
        state = text(pr.get("state") or pr.get("pr_state"), 32)
        checks = text(pr.get("checks"), 32)
        last = text(pr.get("last"), 64)
        if state is None or checks is None or (last is not None and _time(last) is None):
            raise ValueError("facts.prs entries are invalid")
        normalized_prs.append({"pr": pr.get("pr"), "state": state, "draft": bool(pr.get("draft", False)), "checks": checks, "last": last})
    worker = facts.get("worker") or {}
    if not isinstance(worker, dict):
        raise ValueError("facts.worker must be an object")
    worker_state = text(worker.get("state") or facts.get("worker_state"), 64)
    worker_at = text(worker.get("state_at") or facts.get("worker_state_at"), 64)
    if worker_at is not None and _time(worker_at) is None:
        raise ValueError("facts.worker.state_at must be an ISO timestamp")
    markers = facts.get("markers") or {}
    if not isinstance(markers, dict):
        raise ValueError("facts.markers must be an object")
    done = markers.get("done") or facts.get("done") or []
    if not isinstance(done, list) or len(done) > 8 or any(not text(x, 32) for x in done):
        raise ValueError("facts.markers.done is invalid")
    bot = facts.get("bot") or []
    if not isinstance(bot, list) or len(bot) > 100:
        raise ValueError("facts.bot must be a bounded list")
    normalized_bot = []
    for item in bot:
        if not isinstance(item, dict) or not text(item.get("mark"), 64) or _time(item.get("at")) is None:
            raise ValueError("facts.bot entries are invalid")
        normalized_bot.append({"mark": item["mark"], "at": item["at"]})
    release = facts.get("release") or {}
    if not isinstance(release, dict):
        raise ValueError("facts.release must be an object")
    created = text(facts.get("created"), 64)
    if created is not None and _time(created) is None:
        raise ValueError("facts.created must be an ISO timestamp")
    return {"number": number, "labels": labels, "created": created, "human_activity": stamps(facts.get("human_activity"), "human_activity"), "prs": normalized_prs, "markers": {"session": text(markers.get("session"), 191), "done": done}, "bot": normalized_bot, "worker": {"state": worker_state, "state_at": worker_at, "last_activity": text(worker.get("last_activity"), 64), "gate": text(worker.get("gate"), 32)}, "release": {"publishedAt": text(release.get("publishedAt"), 64), "running": bool(release.get("running", False)), "startedAt": text(release.get("startedAt"), 64)}}


def decide(
    facts: dict[str, Any],
    now: datetime,
    policy: ContinuityPolicy | dict[str, Any] | None = None,
) -> Decision:
    """Choose at most one continuity action without reading or writing providers."""
    facts = normalize_facts(facts)
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    policy = normalize_policy(policy)
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
    deadline, deadline_kind = (
        _deadline(facts, policy) if policy.explicit_deadlines else (None, None)
    )
    # Old reconcile behavior uses a 30 minute idle window and starts the next
    # window after a post-activity marker (not the original activity event).
    due = deadline or _legacy_touched(facts, active) + timedelta(
        minutes=policy.stall_minutes
    )
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
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

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
        facts = normalize_facts(facts)
        hub = decide(facts, now, normalize_policy(policy))
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
            with self._lock:
                self.mismatches.append(record)
                del self.mismatches[: -self.limit]
            response["recorded"] = True
        else:
            response["recorded"] = False
        return response


def shadow_compare_service(collector: Any) -> ShadowCompareService:
    """Get one collector-local service without racing threaded HTTP handlers."""
    with _SHADOW_SERVICE_INIT_LOCK:
        service = getattr(collector, "continuity_shadow_compare", None)
        if service is None:
            service = ShadowCompareService()
            collector.continuity_shadow_compare = service
        return service
