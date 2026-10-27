"""Single coordinator work tools over live provider reads and durable intents."""

import hashlib
import json
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import load_policy
from .provider import ItemRef
from .store import stale_sessionless_intent

SYSTEMIC_COMMENT = (
    "Systemic review: the attempt budget is exhausted. Repeated failures show "
    "that the acceptance assumptions remain unverified; the retry process has "
    "not established a successful behavioral check. Inspect linked runs and "
    "record a changed approach before authorizing another attempt."
)


class WorkDenied(RuntimeError):
    """A launch or update refused for a reason safe to show the caller."""


@dataclass(frozen=True)
class WorkActor:
    principal: str
    role: str
    human: bool = False
    host_id: str | None = None


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def code(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}", value
    ):
        raise ValueError("opaque metadata code required")
    return value


def route(accounts, policy, estimate, eligible):
    """Reuse quota hint semantics; each subscription retains its own constraints."""
    from drover.server.mcp.tools import _build_routing_hint

    if (
        isinstance(estimate, bool)
        or not isinstance(estimate, (float, int))
        or not math.isfinite(estimate)
        or estimate <= 0
    ):
        raise ValueError("positive estimated quota percent required")
    now = datetime.now(timezone.utc)
    choices = []
    for account in accounts:
        hint = _build_routing_hint([account], now)
        for harness, utilization in re.findall(
            r"(?:^|;\s*)([a-z-]+): ([0-9.]+)%", hint
        ):
            # Hints round percentages for display. Route with exact constraints.
            from drover.server.providers.quota import is_routing_window

            windows = [
                w
                for w in account.get("windows", [])
                if is_routing_window(account.get("provider"), w.get("kind"))
                and (
                    account.get("provider") != "google"
                    or ("claude_gpt" in w["kind"]) == (harness == "agy-claude")
                )
            ]
            available = 100 - max(w["used_percent"] for w in windows)
            reserve = available * policy.quota.reserve_fraction
            if estimate + reserve <= available:
                for host in account.get("hosts", []):
                    host_id = host if isinstance(host, str) else host.get("host_id")
                    canonical = {"claude": "claude-code", "agy-claude": "agy"}.get(
                        harness, harness
                    )
                    if (host_id, canonical) in eligible:
                        choices.append((available, host_id, canonical))
    if not choices:
        raise WorkDenied("quota or capability unavailable")
    _, host, harness = max(choices)
    return host, harness


class WorkService:
    def __init__(self, config, store, providers, harness):
        self.config, self.store, self.providers, self.harness = (
            config,
            store,
            providers,
            harness,
        )
        self.execution_kill_switch = config.execution.kill_switch
        self.lock = threading.RLock()
        from .github_events import GitHubWebhookReceiver

        self.github_webhooks = GitHubWebhookReceiver(store, config.github_webhook)

    def register_hub_launch(self, con, *, session_id, payload, owner):
        """Register the hub's accepted session before its transaction commits.

        This intentionally bypasses the old queue's shadow-mode mutation gate:
        shadow mode controls automatic provider actions, never observation.
        """
        prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else ""
        intent = payload.get("intent") if isinstance(payload.get("intent"), str) else ""
        branch = (
            payload.get("branch") if isinstance(payload.get("branch"), str) else None
        )
        title = (
            intent
            or (prompt.splitlines()[0] if prompt else "")
            or branch
            or "Session work"
        ).strip()
        repo_owner = payload.get("repo_owner")
        repo_name = payload.get("repo_name")
        repo = (
            f"{repo_owner}/{repo_name}"
            if isinstance(repo_owner, str)
            and isinstance(repo_name, str)
            and repo_owner.strip()
            and repo_name.strip()
            else None
        )
        self.store.register_hub_launch(
            con,
            session_id=session_id,
            title=title[:500] or "Session work",
            repo=repo,
            branch=branch,
            owner=owner or "hub",
            work_item_id=payload.get("work_item_id"),
            acceptance=(
                payload.get("acceptance")
                if isinstance(payload.get("acceptance"), str)
                else None
            ),
        )

    def scope(self, goal_id, number=None):
        scope = next((g for g in self.config.goals if g.goal_id == goal_id), None)
        if scope is None:
            raise PermissionError("goal outside workspace")
        provider = self.providers[goal_id]
        provider.authenticate()
        policy = load_policy(scope.policy_path)
        ref = None if number is None else self.ref(scope, number)
        return scope, provider, policy, ref

    def ref(self, scope, number):
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError("positive issue number required")
        return ItemRef(
            self.config.workspace_id,
            "github",
            scope.repo,
            number,
            f"https://github.com/{scope.repo}/issues/{number}",
        )

    def item(self, goal_id, number):
        scope, provider, policy, ref = self.scope(goal_id, number)
        return (
            provider,
            policy,
            ref,
            self.checked(scope, policy, provider.get_item(ref)),
        )

    @staticmethod
    def checked(scope, policy, item):
        """Reject an item outside the goal or with ambiguous workflow labels."""
        labels = {l["name"] for l in item["labels"]}
        milestone = item.get("milestone") or {}
        if (
            scope.goal_label
            and scope.goal_label not in labels
            or scope.milestone
            and milestone.get("number") != scope.milestone
        ):
            raise PermissionError("item outside goal")
        statuses = {
            l[len(policy.labels.status_prefix) :]
            for l in labels
            if l.startswith(policy.labels.status_prefix)
        }
        if len(statuses) != 1 or not statuses <= set(policy.labels.statuses):
            raise ValueError("invalid status labels")
        for prefix, allowed in (
            (policy.labels.agent_prefix, policy.labels.agents),
            (policy.labels.priority_prefix, policy.labels.priorities),
        ):
            values = {l[len(prefix) :] for l in labels if l.startswith(prefix)}
            if len(values) > 1 or not values <= set(allowed):
                raise ValueError("invalid assignment or priority labels")
        if sum(l.startswith(policy.labels.goal_prefix) for l in labels) > 1:
            raise ValueError("conflicting goal labels")
        return item

    @staticmethod
    def coordinator(actor, policy):
        if actor.role != policy.authority.coordinator:
            raise PermissionError("coordinator required")

    def call(self, operation, args, actor):
        if not actor.principal:
            raise PermissionError("authenticated principal required")
        methods = {
            name: getattr(self, name)
            for name in (
                "list",
                "get",
                "launch",
                "link",
                "update",
                "question",
                "decide",
            )
        }
        if operation not in methods:
            raise ValueError("unknown work operation")
        if self.config.shadow_mode and operation not in {"list", "get"}:
            raise PermissionError("shadow mode is read only")
        # Reject caller identities and all unrecognized fields at the method boundary.
        with self.lock:
            return methods[operation](actor=actor, **args)

    def list(self, goal_id=None, *, actor, limit=50, view=None):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("bounded limit required")
        if view == "attention":
            return self.attention(goal_id, actor=actor, limit=limit)
        if view is not None:
            raise ValueError("unknown work view")
        scope, provider, _, _ = self.scope(goal_id)
        items = provider.list_items()
        return {
            "items": items[:limit],
            "truncated": len(items) > limit,
            "workspace_id": self.config.workspace_id,
            "stale": False,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }

    def attention(self, goal_id, *, actor, limit):
        """Disposable metadata projection; no provider or question text is copied."""
        goals = [
            g for g in self.config.goals if goal_id is None or g.goal_id == goal_id
        ]
        if goal_id is not None and not goals:
            raise PermissionError("goal outside workspace")
        refreshed = datetime.now(timezone.utc).isoformat()
        rows = {}
        intervals = []
        for goal in goals:
            scope, provider, policy, _ = self.scope(goal.goal_id)
            intervals.append(policy.poll.interval_seconds)

            def row(source, kind, url, **metadata):
                key = identity(self.config.workspace_id, scope.repo, source)
                rows.setdefault(
                    key,
                    {
                        "id": key,
                        "workspace_id": self.config.workspace_id,
                        "actor_principal": actor.principal,
                        "goal_id": goal.goal_id,
                        "repo": scope.repo,
                        "kind": kind,
                        "url": url,
                        "refreshed_at": refreshed,
                        "freshness_seconds": policy.poll.freshness_seconds,
                        "stale": False,
                        "actions": [],
                        **metadata,
                    },
                )

            # The issue listing is independent of the PR and run reads.
            with ThreadPoolExecutor(1) as pool:
                listed = pool.submit(provider.list_items)
                flagged = provider.list_attention()
                observed_items = listed.result()
            for entry in flagged:
                row(
                    entry["source_key"],
                    entry["kind"],
                    entry["url"],
                    source_updated_at=entry.get("updated_at"),
                )
            # The listing already carries each issue's labels, state and
            # milestone. Re-reading every item (plus a token check per read)
            # was two GitHub round trips per goal item and pushed this view
            # past the MCP read deadline (drover#778).
            for observed in observed_items:
                try:
                    ref = self.ref(scope, observed["number"])
                    item = self.checked(scope, policy, observed)
                except ValueError:
                    if observed["state"] == "open":
                        row(
                            f"labels:{observed['number']}",
                            "blocked",
                            f"https://github.com/{scope.repo}/issues/{observed['number']}",
                            number=observed["number"],
                            category="inspect_work_labels",
                        )
                    continue
                links = self.store.links(goal_id=goal.goal_id, number=ref.number)
                session_id = next(
                    (l["session_id"] for l in reversed(links) if l["session_id"]), None
                )
                if item["state"] == "open" and any(
                    l["name"] == policy.labels.status_prefix + "blocked"
                    for l in item["labels"]
                ):
                    row(
                        f"blocked:{ref.number}",
                        "blocked",
                        ref.url,
                        number=ref.number,
                        session_id=session_id,
                    )
                for link in links[-1:]:
                    session = (
                        self.harness.resolve(link["run_key"])
                        if link["session_id"]
                        else None
                    )
                    if session and session.status in {"failed", "errored"}:
                        row(
                            "failed:" + link["run_key"],
                            "failed_session",
                            ref.url,
                            number=ref.number,
                            session_id=link["session_id"],
                        )
                for decision in self.store.decisions(ref):
                    if decision["resolved"]:
                        continue
                    actions = []
                    if (
                        actor.human
                        and item["state"] == "open"
                        and not self.config.shadow_mode
                        and not decision["intent_recorded"]
                    ):
                        for action in decision["options"]:
                            if action == "reject" or (
                                action != "open"
                                and provider.inspect_action(ref, action).get(
                                    "supported"
                                )
                            ):
                                actions.append(action)
                    row(
                        "question:" + decision["decision_id"],
                        "agent_question",
                        ref.url,
                        number=ref.number,
                        session_id=session_id,
                        decision_id=decision["decision_id"],
                        actions=actions,
                        category=decision["action_category"],
                        uncertain=decision["intent_recorded"],
                        source_updated_at=decision["created_at"],
                    )
        items = list(rows.values())
        return {
            "items": items[:limit],
            "truncated": len(items) > limit,
            "workspace_id": self.config.workspace_id,
            "poll_interval_seconds": (
                min(intervals) if intervals else load_policy().poll.interval_seconds
            ),
            "stale": False,
            "refreshed_at": refreshed,
        }

    def get(self, goal_id, number, *, actor):
        _, _, ref, item = self.item(goal_id, number)
        return {
            "ref": ref.__dict__,
            "item": item,
            "links": self.store.links(goal_id=goal_id, number=number),
            "decisions": self.store.decisions(ref),
            "stale": False,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }

    def launch(
        self,
        goal_id,
        number,
        request_key,
        estimated_cost,
        *,
        actor,
        run_role="work",
        parent_session_id=None,
    ):
        provider, policy, ref, item = self.item(goal_id, number)
        self.coordinator(actor, policy)
        code(request_key)
        if run_role not in {"work", "verify"}:
            raise ValueError("invalid run role")
        run_key = identity(
            self.config.workspace_id, goal_id, number, request_key, run_role
        )
        existing = self.store.links(run_key=run_key)
        if existing:
            row = existing[0]
            if not row["session_id"]:
                session = self.harness.resolve(run_key)
                if session is None:
                    if not stale_sessionless_intent(
                        row, minutes=self.config.execution.orphan_intent_minutes
                    ):
                        raise WorkDenied("launch intent uncertain; inspection required")
                    failures = row["failure_count"] + 1
                    self.store.failure(run_key, failures)
                    if failures >= policy.retries.attempts:
                        raise WorkDenied(
                            "systemic review required before further attempts"
                        )
                    # Re-enter the normal launch path with the durable intent row.
                    # ``intent`` is idempotent on run_key, so no duplicate is created.
                    existing = []
                else:
                    row = self.store.associate(run_key, session.session_id)
            if existing and (
                run_role == "work"
                and item["state"] == "open"
                and policy.labels.status_prefix + "queued"
                in {l["name"] for l in item["labels"]}
            ):
                self.status(provider, policy, ref, item, "running")
            if existing:
                return row
        if item["state"] != "open":
            raise ValueError("closed item cannot launch")
        links = self.store.links(goal_id=goal_id, number=number)
        failures = max((l["failure_count"] for l in links), default=0)
        if failures >= policy.retries.attempts:
            raise WorkDenied("systemic review required before further attempts")
        status = policy.labels.status_prefix + (
            "review" if run_role == "verify" else "queued"
        )
        if status not in {l["name"] for l in item["labels"]}:
            raise ValueError("item not ready for this run role")
        active = self.harness.active(self.store.links())
        if (
            len(active) >= policy.capacity.workspace_workers
            or sum(l["goal_id"] == goal_id for l in active)
            >= policy.capacity.goal_workers
        ):
            raise WorkDenied("worker capacity unavailable")
        # A finalised worker frees its slot but its session is still resumable,
        # so it keeps blocking a second execution of the same item.
        if any(
            l["goal_id"] == goal_id
            and l["item_number"] == number
            and not stale_sessionless_intent(
                l, minutes=self.config.execution.orphan_intent_minutes
            )
            for l in self.harness.active(self.store.links(), release_finalised=False)
        ):
            raise WorkDenied("existing execution requires inspection")
        if run_role == "verify":
            parent = next(
                (
                    l
                    for l in links
                    if l["session_id"] == parent_session_id and l["run_role"] == "work"
                ),
                None,
            )
            if not parent or request_key != identity(
                parent["run_key"], "verify", item.get("body")
            ):
                raise ValueError(
                    "verification must reference work and current criteria"
                )
        host, harness = route(
            self.harness.quota(),
            policy,
            estimated_cost,
            self.harness.eligible(goal_id, parent_session_id),
        )
        if parent_session_id is not None and not any(
            l["session_id"] == parent_session_id for l in self.store.links()
        ):
            raise PermissionError("parent session outside workspace")
        scope = next(g for g in self.config.goals if g.goal_id == goal_id)
        goal_ref = scope.goal_label or f"milestone:{scope.milestone}"
        row = self.store.intent(
            actor.principal, ref, goal_id, run_key, run_role, failures, goal_ref
        )
        # The intent is committed before this call. Any exception leaves it inspectable.
        session_id = self.harness.launch(
            host,
            harness,
            run_key,
            ref,
            run_role,
            parent_session_id,
            goal_id,
            authority={
                "labels": [label["name"] for label in item.get("labels", [])],
                "body": item.get("body"),
            },
        )
        linked = self.store.associate(row["run_key"], session_id)
        if run_role == "work":
            self.status(provider, policy, ref, item, "running")
        return linked

    def link(self, goal_id, number, run_key, session_id, *, actor, pr_number=None):
        _, policy, ref, _ = self.item(goal_id, number)
        self.coordinator(actor, policy)
        rows = self.store.links(run_key=code(run_key))
        if (
            not rows
            or rows[0]["goal_id"] != goal_id
            or rows[0]["item_number"] != number
        ):
            raise PermissionError("link outside item")
        session = self.harness.resolve(run_key)
        if session is None or session.session_id != session_id:
            raise ValueError("session must resolve persisted run key")
        pr = None
        if pr_number is not None:
            if (
                isinstance(pr_number, bool)
                or not isinstance(pr_number, int)
                or pr_number <= 0
            ):
                raise ValueError("positive PR number required")
            pr = f"https://github.com/{ref.repo}/pull/{pr_number}"
        return self.store.associate(run_key, session_id, pr)

    @staticmethod
    def status(provider, policy, ref, item, status):
        labels = [
            l["name"]
            for l in item["labels"]
            if not l["name"].startswith(policy.labels.status_prefix)
        ]
        provider.set_labels(ref, labels + [policy.labels.status_prefix + status])

    def update(
        self,
        goal_id,
        number,
        run_key,
        action,
        *,
        actor,
        estimated_cost=None,
        behavioral_passed=None,
        checks_rerun=None,
        evidence_key=None,
    ):
        provider, policy, ref, item = self.item(goal_id, number)
        rows = self.store.links(run_key=code(run_key))
        if (
            not rows
            or rows[0]["goal_id"] != goal_id
            or rows[0]["item_number"] != number
        ):
            raise PermissionError("execution outside item")
        if actor.role != policy.authority.coordinator:
            session = self.harness.resolve(run_key)
            if (
                action == "verified"
                or not actor.host_id
                or not session
                or session.host_id != actor.host_id
            ):
                raise PermissionError("execution reporting credential required")
        row = rows[0]
        if evidence_key is not None:
            code(evidence_key)
        key = identity(run_key, action, evidence_key if action == "progress" else None)
        events = self.store.events(key)
        if any(e["event_kind"] == "outcome" for e in events):
            return {"outcome": events[-1]["outcome"]}
        if events:
            raise WorkDenied("provider operation uncertain; inspection required")
        if item["state"] != "open":
            raise ValueError("item already closed")
        if action == "complete":
            self.coordinator(actor, policy)
            if row["run_role"] != "work":
                raise ValueError("verification needs behavioral result")
            request = identity(run_key, "verify", item.get("body"))
            verification_key = identity(
                self.config.workspace_id, goal_id, number, request, "verify"
            )
            if not self.store.links(run_key=verification_key):
                if (
                    max(
                        l["failure_count"]
                        for l in self.store.links(goal_id=goal_id, number=number)
                    )
                    >= policy.retries.attempts
                ):
                    raise WorkDenied("systemic review required before verification")
                session = self.harness.resolve(run_key)
                if session is None or session.status != "completed":
                    raise ValueError("work session must complete before verification")
                self.status(provider, policy, ref, item, "review")
            return self.launch(
                goal_id,
                number,
                request,
                estimated_cost,
                actor=actor,
                run_role="verify",
                parent_session_id=row["session_id"],
            )
        if action not in {"verified", "failed", "progress"}:
            raise ValueError("unsupported update action")
        if action == "verified":
            if row["run_role"] != "verify" or not row["session_id"]:
                raise ValueError("separate verification session required")
            acceptance = policy.acceptance
            if not (
                acceptance.separate_verification_run
                and acceptance.behavioral_evidence_required
                and acceptance.rerun_relevant_checks
            ):
                raise PermissionError(
                    "unsupported acceptance policy; inspection required"
                )
            if (
                behavioral_passed is not True
                or checks_rerun is not True
                or not evidence_key
            ):
                raise ValueError(
                    "successful behavioral evidence and rerun checks required"
                )
            code(evidence_key)
            session = self.harness.resolve(run_key)
            if session is None or session.status != "completed":
                raise ValueError("verification session must complete before acceptance")
            # Criteria changes invalidate this verification's stable identity.
            expected = identity(
                self.config.workspace_id,
                goal_id,
                number,
                identity(
                    self._work_parent(
                        links=self.store.links(goal_id=goal_id, number=number), row=row
                    ),
                    "verify",
                    item.get("body"),
                ),
                "verify",
            )
            if run_key != expected:
                raise ValueError(
                    "acceptance criteria changed; new verification required"
                )
        self.store.event(actor.principal, ref, key, "intent", action, link_id=row["id"])
        if action == "verified":
            provider.close(ref)
        elif action == "failed":
            failures = (
                max(
                    l["failure_count"]
                    for l in self.store.links(goal_id=goal_id, number=number)
                )
                + 1
            )
            self.store.failure(run_key, failures)
            blocked = failures >= policy.retries.attempts
            self.status(provider, policy, ref, item, "blocked" if blocked else "queued")
            if blocked and policy.retries.systemic_review_required:
                provider.comment(ref, SYSTEMIC_COMMENT)
        if action == "progress":
            self.store.associate(run_key, row["session_id"])
        self.store.event(
            actor.principal,
            ref,
            key,
            "outcome",
            action,
            outcome=action,
            operation_id=evidence_key if action == "verified" else None,
            link_id=row["id"],
        )
        return {"outcome": action}

    def _work_parent(self, links, row):
        session = self.harness.resolve(row["run_key"])
        return next(
            (
                l["run_key"]
                for l in links
                if session and l["session_id"] == session.parent_session_id
            ),
            None,
        )

    def question(self, goal_id, number, request_key, category, options, *, actor):
        _, policy, ref, _ = self.item(goal_id, number)
        code(request_key)
        if category not in {
            *policy.authority.human_actions,
            *policy.authority.allowed_actions,
            "revision",
            "inspection",
        }:
            raise ValueError("unknown action category")
        if (
            not options
            or len(options) > 10
            or not set(options) <= {"approve", "reject", "open", "retry"}
        ):
            raise ValueError("supported action codes required")
        key = identity(self.config.workspace_id, goal_id, number, request_key)
        events = self.store.events(key)
        if not events:
            self.store.event(actor.principal, ref, key, "question", category, options)
        elif (
            events[0]["repo"],
            events[0]["item_number"],
            events[0]["action_category"],
            events[0]["options"],
        ) != (ref.repo, number, category, options):
            raise ValueError("question identity conflict")
        return {
            "decision_id": key,
            "resolved": any(e["event_kind"] == "outcome" for e in events),
        }

    def decide(self, goal_id, number, decision_id, action, *, actor):
        provider, policy, ref, item = self.item(goal_id, number)
        if not actor.human:
            raise PermissionError("paired human decision required")
        events = self.store.events(code(decision_id))
        if (
            not events
            or events[0]["repo"] != ref.repo
            or events[0]["item_number"] != number
        ):
            raise PermissionError("decision outside item")
        if any(e["event_kind"] == "outcome" for e in events):
            return {"outcome": events[-1]["outcome"], "resolved": True}
        if len(events) > 1:
            raise WorkDenied("decision uncertain; provider inspection required")
        if action not in events[0]["options"]:
            raise ValueError("action not offered")
        if action == "open":
            return {"url": ref.url, "resolved": False}
        if action != "reject":
            inspected = provider.inspect_action(ref, action)
            if not inspected.get("supported"):
                return {"url": ref.url, "resolved": False, "supported": False}
        self.store.event(
            actor.principal,
            ref,
            decision_id,
            "intent",
            events[0]["action_category"],
            outcome=action,
        )
        if action == "reject":
            self.status(provider, policy, ref, item, "queued")
        else:
            provider.execute_action(ref, action)
        self.store.event(
            actor.principal,
            ref,
            decision_id,
            "outcome",
            events[0]["action_category"],
            outcome=action,
        )
        return {"outcome": action, "resolved": True}
