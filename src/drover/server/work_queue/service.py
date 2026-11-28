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
    # Only a device credential redeemed through the pairing flow has this
    # provenance.  ``human`` alone is deliberately insufficient: it is also
    # used by legacy/operator compatibility paths.
    paired_human: bool = False


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def verification_identity(workspace_id, goal_id, number, parent_run_key, criteria):
    """Return ``(request_key, run_key)`` for one separate verification.

    The single derivation shared by launch, deferred completion/verification
    and acceptance. Callers resolve ``criteria`` themselves; this is pure.
    """
    request = identity(parent_run_key, "verify", criteria)
    return request, identity(workspace_id, goal_id, number, request, "verify")


def code(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}", value
    ):
        raise ValueError("opaque metadata code required")
    return value


def route(accounts, policy, estimate, eligible):
    """Reuse quota hint semantics; each subscription retains its own constraints."""
    from drover.server.providers.quota_read import _build_routing_hint

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
            scope.candidate_source,
            scope.repo,
            number,
            (
                f"local://{scope.goal_id}/{number}"
                if scope.candidate_source == "local_file"
                else f"https://github.com/{scope.repo}/issues/{number}"
            ),
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
                "bind_existing",
                "update",
                "question",
                "decide",
                "delegate",
                "revoke_delegation",
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
        verified_terminal_parent=False,
        delegation_id=None,
    ):
        provider, policy, ref, item = self.item(goal_id, number)
        code(request_key)
        if delegation_id is None:
            self.coordinator(actor, policy)
        else:
            self.delegation_allowed(
                delegation_id,
                actor,
                ref,
                action="launch",
                stage=run_role,
                operation_key=request_key,
            )
        if run_role not in {"work", "verify"}:
            raise ValueError("invalid run role")
        run_key = identity(
            self.config.workspace_id, goal_id, number, request_key, run_role
        )
        deferred_binding = None
        preflight = getattr(self.harness, "reserve_work_coordinator_launch", None)
        existing = self.store.links(run_key=run_key)
        if existing:
            row = existing[0]
            if not row["session_id"]:
                session = self.harness.resolve(run_key)
                if session is None:
                    # A signed serialized launch that has not reached the
                    # daemon is a known, safe deferral while another run owns
                    # the integration key. Re-check the immutable envelope;
                    # a released matching reservation lets this exact intent
                    # proceed without creating an orphan session.
                    raw_binding = row.get("work_coordinator_binding_json")
                    if raw_binding is not None and callable(preflight):
                        try:
                            candidate_binding = json.loads(raw_binding)
                        except (TypeError, ValueError, json.JSONDecodeError) as exc:
                            raise WorkDenied(
                                "stored work continuity binding is invalid; inspection required"
                            ) from exc
                        if not isinstance(candidate_binding, dict):
                            raise WorkDenied(
                                "stored work continuity binding is invalid; inspection required"
                            )
                        if preflight(candidate_binding):
                            deferred_binding = candidate_binding
                            existing = []
                    if existing:
                        if not stale_sessionless_intent(
                            row, minutes=self.config.execution.orphan_intent_minutes
                        ):
                            raise WorkDenied(
                                "launch intent uncertain; inspection required"
                            )
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
        parent = None
        if run_role == "verify":
            parent = next(
                (
                    l
                    for l in links
                    if l["session_id"] == parent_session_id and l["run_role"] == "work"
                ),
                None,
            )
            if (
                not parent
                or request_key
                != verification_identity(
                    self.config.workspace_id,
                    goal_id,
                    number,
                    parent["run_key"],
                    self._verification_criteria(provider, ref, item),
                )[0]
            ):
                raise ValueError(
                    "verification must reference work and current criteria"
                )
        elif verified_terminal_parent:
            raise ValueError("only verification may release a terminal parent")
        active = [
            link
            for link in self.harness.active(self.store.links())
            if link["run_key"] != run_key
        ]
        if (
            len(active) >= policy.capacity.workspace_workers
            or sum(l["goal_id"] == goal_id for l in active)
            >= policy.capacity.goal_workers
        ):
            raise WorkDenied("worker capacity unavailable")
        # A finalised worker frees its slot but its session is still resumable,
        # so it keeps blocking a second execution of the same item.
        if any(
            l["run_key"] != run_key
            and l["goal_id"] == goal_id
            and l["item_number"] == number
            and not stale_sessionless_intent(
                l, minutes=self.config.execution.orphan_intent_minutes
            )
            and not (
                verified_terminal_parent
                and run_role == "verify"
                and parent is not None
                and l["run_key"] == parent["run_key"]
                and l["session_id"] == parent_session_id
            )
            for l in self.harness.active(self.store.links(), release_finalised=False)
        ):
            raise WorkDenied("existing execution requires inspection")
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
        authority = {
            "labels": item.get("operator_authority_labels", []),
            "body": item.get("body"),
            "local_candidate_number": number,
            "local_candidate_cwd": scope.local_candidate_worktrees.get(str(number)),
            "expected_effect_artifact": item.get("expected_effect_artifact"),
        }
        prepare_binding = getattr(
            self.harness, "prepare_work_coordinator_binding", None
        )
        binding = deferred_binding or (
            prepare_binding(run_key, run_role, ref, goal_id, authority=authority)
            if callable(prepare_binding)
            else None
        )
        row = self.store.intent(
            actor.principal, ref, goal_id, run_key, run_role, failures, goal_ref
        )
        if binding is not None:
            binding = self.store.bind_work_coordinator_launch(run_key, binding)
            if not deferred_binding and callable(preflight):
                preflight(binding)
        # The intent and, when applicable, its signed continuity envelope are
        # committed before this call. Any exception leaves them inspectable.
        launch_kwargs = {"authority": authority}
        if binding is not None:
            launch_kwargs["continuity_binding"] = binding
        session_id = self.harness.launch(
            host,
            harness,
            run_key,
            ref,
            run_role,
            parent_session_id,
            goal_id,
            **launch_kwargs,
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

    def _existing_session_binding(self, row):
        binding = self.store.existing_session_binding(
            identity(row["run_key"], "bind-existing-session"), row["id"]
        )
        if binding is None:
            return None
        required = {
            "role",
            "session_id",
            "turn_id",
            "profile_revision",
            "source_head",
        }
        if not required <= set(binding) or binding["role"] not in {"work", "verify"}:
            raise WorkDenied("stored existing session binding is invalid")
        if row["run_role"] != binding["role"] or (
            row["session_id"] is not None and row["session_id"] != binding["session_id"]
        ):
            raise WorkDenied("stored existing session binding is invalid")
        return binding

    def _bound_session(self, scope, row):
        binding = self._existing_session_binding(row)
        if binding is None:
            return self.harness.resolve(row["run_key"])
        if self._current_existing_session_binding(scope, row, binding) is None:
            return None
        reader = getattr(self.harness, "existing_session", None)
        if not callable(reader):
            return None
        session = reader(binding["session_id"], binding["turn_id"], scope.repo)
        if session is None or session.session_id != binding["session_id"]:
            return None
        return session

    def _current_existing_session_binding(self, scope, row, binding=None):
        """Return an existing binding only while installed policy selects it.

        A link-keyed binding remains immutable audit history after a later
        reviewed input turn replaces it.  It must not remain eligible merely
        because its old session is still readable.
        """
        binding = (
            binding if binding is not None else self._existing_session_binding(row)
        )
        if binding is None:
            return None
        profile = scope.github_acceptance_profiles.get(str(row["item_number"]))
        configured = scope.github_existing_session_bindings.get(str(row["item_number"]))
        if (
            profile is None
            or configured is None
            or configured.profile_revision != profile.revision
        ):
            return None
        work_request = identity(
            "existing-session",
            configured.profile_revision,
            configured.source_head,
            configured.work_session_id,
            configured.work_turn_id,
        )
        work_key = identity(
            self.config.workspace_id,
            scope.goal_id,
            row["item_number"],
            work_request,
            "work",
        )
        expected = self._existing_binding_payload(
            configured,
            role=row["run_role"],
            parent_run_key=work_key if row["run_role"] == "verify" else None,
        )
        return binding if binding == expected else None

    def _existing_pair_is_replaceable(self, links):
        """Allow a new reviewed pair only before any old pair effect/intention.

        Old links and their immutable policy events remain audit history.  A
        provider intent, outcome, progress, or any malformed extra event means
        an effect may be uncertain and requires inspection instead.
        """
        for link in links:
            if self._existing_session_binding(link) is None:
                return False
            events = self.store.link_events(link["id"])
            if len(events) != 1:
                return False
            event = events[0]
            if (
                event["event_kind"] != "policy"
                or event["action_category"] != "bind_existing_session"
                or event["outcome"] != "bound"
            ):
                return False
        return True

    def _associate_existing_session_reference(self, row, binding):
        """Associate once, then retain later reviewed turns in binding evidence.

        ``execution_links`` deliberately permits one owner per session.  An
        existing-session binding already contains the immutable session ID, so
        a later reviewed turn of the same session keeps its link unassociated
        rather than rewriting its old owner or weakening that uniqueness rule.
        """
        session_id = binding["session_id"]
        if row["session_id"] == session_id:
            return row
        if row["session_id"] is not None:
            raise WorkDenied("existing session association conflict")
        prior = self.store.session_link(session_id)
        if prior is None:
            return self.store.associate(row["run_key"], session_id)
        if prior["id"] == row["id"]:
            raise WorkDenied("existing session association conflict")
        prior_binding = self._existing_session_binding(prior)
        required = ("role", "session_id", "profile_revision", "source_head")
        if (
            prior_binding is None
            or prior["goal_id"] != row["goal_id"]
            or prior["item_number"] != row["item_number"]
            or prior["provider"] != row["provider"]
            or prior["repo"] != row["repo"]
            or any(prior_binding.get(key) != binding.get(key) for key in required)
        ):
            raise WorkDenied("existing session association requires inspection")
        return row

    def _acceptance_session_is_settled(self, scope, row, session):
        """Accept a running lifetime only through the strict existing reader."""
        if row is None:
            return False
        binding = self._existing_session_binding(row)
        if binding is None:
            return session is not None and session.status == "completed"
        return (
            session is not None
            and self._current_existing_session_binding(scope, row, binding) is not None
        )

    def _existing_binding_payload(self, binding, *, role, parent_run_key=None):
        field_prefix = "verifier" if role == "verify" else role
        payload = {
            "role": role,
            "session_id": getattr(binding, field_prefix + "_session_id"),
            "turn_id": getattr(binding, field_prefix + "_turn_id"),
            "profile_revision": binding.profile_revision,
            "source_head": binding.source_head,
        }
        if parent_run_key is not None:
            payload["parent_run_key"] = parent_run_key
        return payload

    def bind_existing(self, goal_id, number, *, actor):
        """Bind one reviewed, completed session pair without launching work.

        Only the installed exact-issue configuration chooses session and turn
        identifiers. The request deliberately carries neither so a caller
        cannot attach an arbitrary transcript to a GitHub close decision.
        """
        provider, policy, ref, item = self.item(goal_id, number)
        self.coordinator(actor, policy)
        scope = next(scope for scope in self.config.goals if scope.goal_id == goal_id)
        profile = scope.github_acceptance_profiles.get(str(number))
        binding = scope.github_existing_session_bindings.get(str(number))
        if profile is None or binding is None:
            raise PermissionError("existing session binding is not installed")
        if binding.profile_revision != profile.revision:
            raise WorkDenied("existing session binding profile revision mismatch")
        labels = {label["name"] for label in item["labels"]}
        if policy.labels.status_prefix + "review" not in labels:
            raise ValueError("item not ready for existing verification")
        source = self._verification_criteria(provider, ref, item)
        if source.get("head") != binding.source_head:
            raise ValueError("acceptance source changed; new verification required")
        reader = getattr(self.harness, "existing_session", None)
        if not callable(reader):
            raise WorkDenied("existing session inspection unavailable")
        work_session = reader(binding.work_session_id, binding.work_turn_id, scope.repo)
        if work_session is None:
            raise ValueError("existing work session lacks terminal provenance")
        verifier_session = reader(
            binding.verifier_session_id, binding.verifier_turn_id, scope.repo
        )
        if verifier_session is None:
            raise ValueError("existing verification session lacks terminal provenance")
        if verifier_session.session_id == work_session.session_id:
            raise ValueError("existing work and verifier sessions must be separate")

        work_request = identity(
            "existing-session",
            binding.profile_revision,
            binding.source_head,
            binding.work_session_id,
            binding.work_turn_id,
        )
        work_key = identity(
            self.config.workspace_id, goal_id, number, work_request, "work"
        )
        _, verify_key = verification_identity(
            self.config.workspace_id, goal_id, number, work_key, source
        )
        links = self.store.links(goal_id=goal_id, number=number)
        if any(
            link["run_key"] not in {work_key, verify_key} for link in links
        ) and not self._existing_pair_is_replaceable(links):
            raise WorkDenied("existing execution requires inspection")
        goal_ref = scope.goal_label or f"milestone:{scope.milestone}"
        work = self.store.intent(
            actor.principal, ref, goal_id, work_key, "work", 0, goal_ref
        )
        self.store.bind_existing_session(
            actor.principal,
            ref,
            identity(work_key, "bind-existing-session"),
            work["id"],
            self._existing_binding_payload(binding, role="work"),
        )
        work = self._associate_existing_session_reference(
            work,
            self._existing_binding_payload(binding, role="work"),
        )
        verify = self.store.intent(
            actor.principal, ref, goal_id, verify_key, "verify", 0, goal_ref
        )
        self.store.bind_existing_session(
            actor.principal,
            ref,
            identity(verify_key, "bind-existing-session"),
            verify["id"],
            self._existing_binding_payload(
                binding, role="verify", parent_run_key=work_key
            ),
        )
        verify = self._associate_existing_session_reference(
            verify,
            self._existing_binding_payload(
                binding, role="verify", parent_run_key=work_key
            ),
        )
        return {"links": [work, verify]}

    @staticmethod
    def status(provider, policy, ref, item, status):
        labels = [
            l["name"]
            for l in item["labels"]
            if not l["name"].startswith(policy.labels.status_prefix)
        ]
        provider.set_labels(ref, labels + [policy.labels.status_prefix + status])

    def _signed_committed_provenance(self, run_key):
        """Read the immutable worker-mode bit; unknown never schedules review."""
        reader = getattr(self.harness, "work_coordinator_committed_provenance", None)
        if not callable(reader):
            return None
        try:
            value = reader(run_key)
        except Exception:
            return None
        return value if isinstance(value, bool) else None

    def _github_event_delivery(self, scope, number, run_key):
        """Require both installed and launch-signed exact-profile opt-ins."""
        profile = scope.github_acceptance_profiles.get(str(number))
        revision = scope.github_event_delivery_profiles.get(str(number))
        reader = getattr(self.harness, "github_event_delivery", None)
        if profile is None or revision != profile.revision or not callable(reader):
            return False
        try:
            return reader(run_key, revision) is True
        except Exception:
            return False

    @staticmethod
    def _github_native_owner(scope):
        owner = "drover:owner/" + scope.goal_id
        return owner if scope.continuity_owner_id == owner else None

    def _queue_github_native_owner_wake(
        self, scope, provider, ref, parent, verifier, facts
    ) -> bool | None:
        """Append one owner wake from a retained, exact GitHub outcome.

        The Factory run belongs to the signed parent work launch.  It is never
        reconstructed from issue text, and this method deliberately leaves a
        missing ledger wake retryable for the signed CI delivery path.
        """
        owner = self._github_native_owner(scope)
        profile = scope.github_acceptance_profiles.get(str(ref.number))
        if (
            owner is None
            or profile is None
            or scope.github_event_delivery_profiles.get(str(ref.number))
            != profile.revision
            or not self._github_event_delivery(scope, ref.number, parent)
            or not self._github_event_delivery(scope, ref.number, verifier)
        ):
            return None
        try:
            retained = json.dumps(facts, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return False
        digest = hashlib.sha256(retained.encode()).hexdigest()
        writer = getattr(self.harness, "github_native_owner_wake", None)
        if not callable(writer):
            return False
        try:
            wake = writer(
                parent,
                source_event_id=digest,
                subject=ref.url,
                summary="GitHub provider verdict "
                + digest
                + " is ready for owner ACK.",
            )
        except Exception:
            return False
        return True if isinstance(wake, str) and wake else False

    def _github_verified_outcome(self, scope, ref, parent):
        """Return the one verified child and its immutable provider facts."""
        links = self.store.links(goal_id=scope.goal_id, number=ref.number)
        children = [
            row
            for row in links
            if row["run_role"] == "verify"
            and self._work_parent(links=links, row=row) == parent
        ]
        if len(children) != 1:
            return None
        verifier = children[0]
        events = self.store.events(identity(verifier["run_key"], "verified", None))
        outcomes = [
            event
            for event in events
            if event["event_kind"] == "outcome"
            and event["action_category"] == "verified"
            and event["outcome"] == "verified"
        ]
        if len(outcomes) != 1:
            return None
        try:
            facts = json.loads(outcomes[0]["provider_operation_id"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return verifier, facts

    def advance_github_native_owner(self, scope, provider, ref):
        """Recreate an unacknowledged native wake from a retained outcome.

        The signed CI worker uses this only as bounded recovery after an
        already-received push.  It never polls GitHub to discover new work and
        never turns an outcome into an acknowledgement without the native
        runner's own effect receipt.
        """
        pending = False
        for row in self.store.links(goal_id=scope.goal_id, number=ref.number):
            if row["run_role"] != "work":
                continue
            verified = self._github_verified_outcome(scope, ref, row["run_key"])
            if verified is None:
                continue
            verifier, facts = verified
            queued = self._queue_github_native_owner_wake(
                scope, provider, ref, row["run_key"], verifier["run_key"], facts
            )
            pending = pending or queued is False
        return "pending" if pending else "settled"

    def github_native_owner_effect(
        self,
        goal_id,
        continuity_run_id,
        *,
        wake_id,
        action_fingerprint,
        owner_id,
        owner_epoch,
    ):
        """Revalidate an exact GitHub verdict before the native owner ACKs it.

        The returned digest is the only artifact a native owner may receipt for
        this path.  It is derived from two fresh provider reads plus the
        immutable decision outcome; it cannot be selected by issue text or the
        owner action.  ``None`` leaves the already-delivered wake retryable.
        """
        if (
            not isinstance(continuity_run_id, str)
            or not continuity_run_id.startswith("work-")
            or not isinstance(wake_id, str)
            or not isinstance(action_fingerprint, str)
            or not isinstance(owner_epoch, int)
        ):
            return None
        parent = continuity_run_id.removeprefix("work-")
        with self.lock:
            scope = next(
                (
                    candidate
                    for candidate in self.config.goals
                    if candidate.goal_id == goal_id
                ),
                None,
            )
            owner = self._github_native_owner(scope) if scope is not None else None
            if owner is None or owner != owner_id:
                return None
            rows = self.store.links(run_key=parent)
            if len(rows) != 1 or rows[0]["run_role"] != "work":
                return None
            row = rows[0]
            if row["goal_id"] != goal_id:
                return None
            try:
                _, provider, _, ref = self.scope(goal_id, row["item_number"])
            except (KeyError, ValueError):
                return None
            profile = scope.github_acceptance_profiles.get(str(ref.number))
            if (
                profile is None
                or scope.github_event_delivery_profiles.get(str(ref.number))
                != profile.revision
                or not self._github_event_delivery(scope, ref.number, parent)
            ):
                return None
            verified = self._github_verified_outcome(scope, ref, parent)
            if verified is None:
                return None
            verifier, retained = verified
            terminal_done = getattr(self.harness, "terminal_work_done", None)
            if (
                not callable(terminal_done)
                or terminal_done(parent) is not True
                or terminal_done(verifier["run_key"]) is not True
                or not self._github_event_delivery(
                    scope, ref.number, verifier["run_key"]
                )
            ):
                return None
            try:
                source = provider.acceptance_source(ref, allow_closed=True)
                evidence = retained["evidence"]
                current = provider.acceptance_facts(ref, evidence, allow_closed=True)
                repeated = provider.acceptance_facts(ref, evidence, allow_closed=True)
                item = provider.get_item(ref)
            except (KeyError, PermissionError, ValueError):
                return None
            if (
                item.get("state") != "closed"
                or source
                != {
                    key: retained.get(key)
                    for key in ("profile", "head", "branch", "pull", "repo")
                }
                or json.dumps(current, sort_keys=True)
                != json.dumps(retained, sort_keys=True)
                or json.dumps(repeated, sort_keys=True)
                != json.dumps(current, sort_keys=True)
            ):
                return None
            _, expected = verification_identity(
                self.config.workspace_id, goal_id, ref.number, parent, source
            )
            if expected != verifier["run_key"]:
                return None
            material = json.dumps(
                {
                    "action": action_fingerprint,
                    "owner": owner,
                    "run": continuity_run_id,
                    "wake": wake_id,
                    "facts": current,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            return (
                "github-native-owner:" + hashlib.sha256(material.encode()).hexdigest()
            )

    def _recover_observed_github_close(self, scope, provider, ref, row, key):
        """ACK an observed close only after current provider facts still agree.

        This is the narrow post-effect/pre-outcome recovery fence. It never
        repeats the close and it is not the native-owner effect/ACK protocol.
        """
        profile = scope.github_acceptance_profiles.get(str(ref.number))
        events = self.store.events(key)
        if (
            profile is None
            or row["run_role"] != "verify"
            or len(events) != 1
            or events[0]["event_kind"] != "intent"
            or events[0]["action_category"] != "verified"
        ):
            return None
        try:
            retained = json.loads(events[0]["provider_operation_id"])
            source = {
                field: retained[field]
                for field in ("profile", "head", "branch", "pull", "repo")
            }
            evidence = retained["evidence"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            source["profile"] != profile.revision
            or source["repo"] != ref.repo
            or source["pull"] != profile.pull_number
            or not isinstance(evidence, str)
        ):
            return None
        links = self.store.links(goal_id=scope.goal_id, number=ref.number)
        parent = self._work_parent(links=links, row=row)
        parent_row = next((link for link in links if link["run_key"] == parent), None)
        terminal_done = getattr(self.harness, "terminal_work_done", None)
        if (
            parent is None
            or parent_row is None
            or not callable(terminal_done)
            or terminal_done(row["run_key"]) is not True
            or terminal_done(parent) is not True
            or not self._github_event_delivery(scope, ref.number, row["run_key"])
            or not self._github_event_delivery(scope, ref.number, parent)
        ):
            return None
        _, expected = verification_identity(
            self.config.workspace_id, scope.goal_id, ref.number, parent, source
        )
        if expected != row["run_key"]:
            return None
        item = provider.get_item(ref)
        if item.get("state") != "closed":
            return None
        try:
            current = provider.acceptance_facts(ref, evidence, allow_closed=True)
        except (PermissionError, ValueError):
            return None
        canonical_current = json.dumps(current, sort_keys=True)
        if (
            canonical_current != json.dumps(retained, sort_keys=True)
            or json.dumps(
                provider.acceptance_facts(ref, evidence, allow_closed=True),
                sort_keys=True,
            )
            != canonical_current
        ):
            return None
        self.store.event(
            "control:queue-execution",
            ref,
            key,
            "outcome",
            "verified",
            outcome="verified",
            operation_id=json.dumps(current, sort_keys=True),
            link_id=row["id"],
        )
        self._queue_github_native_owner_wake(
            scope, provider, ref, parent, row["run_key"], current
        )
        return {"outcome": "verified"}

    def _bound_host_verification_report(self, scope, row, run_key, session, actor):
        """Allow only a bound verifier to defer its own terminal acceptance.

        The parent work link carries the immutable committed-provenance bit.
        A verifier's mutable prompt, goal configuration, or host role cannot
        reconstruct that authority after launch.
        """
        local_delivery = scope.candidate_source == "local_file"
        github_delivery = self._github_event_delivery(
            scope, row["item_number"], row["run_key"]
        )
        if (
            not local_delivery
            and not github_delivery
            or row["run_role"] != "verify"
            or session is None
            or session.status != "running"
            or actor.human
            or not actor.host_id
            or actor.host_id != session.host_id
        ):
            return False
        parent = self._work_parent(
            links=self.store.links(goal_id=scope.goal_id, number=row["item_number"]),
            row=row,
        )
        if parent is None:
            return False
        return (
            local_delivery and self._signed_committed_provenance(parent) is True
        ) or (
            github_delivery
            and self._github_event_delivery(scope, row["item_number"], parent)
        )

    def _verification_criteria(self, provider, ref, item):
        scope = next(
            g for g in self.config.goals if self.providers[g.goal_id] is provider
        )
        if str(ref.number) in scope.github_acceptance_profiles:
            if provider.scope.github_acceptance_profiles.get(str(ref.number)) != (
                scope.github_acceptance_profiles[str(ref.number)]
            ):
                raise PermissionError("acceptance provider profile is stale")
            return provider.acceptance_source(ref)
        return item.get("body")

    def _verification_request(self, goal_id, number, row, item):
        scope = next(g for g in self.config.goals if g.goal_id == goal_id)
        parent_run_key = row["run_key"]
        criteria = self._verification_criteria(
            self.providers[goal_id], self.ref(scope, number), item
        )
        return verification_identity(
            self.config.workspace_id, goal_id, number, parent_run_key, criteria
        )

    def _launch_verification(
        self, provider, policy, ref, item, row, *, goal_id, number, estimated_cost
    ):
        request, verification_key = self._verification_request(
            goal_id, number, row, item
        )
        existing = self.store.links(run_key=verification_key)
        if existing:
            return existing[0]
        if (
            max(
                link["failure_count"]
                for link in self.store.links(goal_id=goal_id, number=number)
            )
            >= policy.retries.attempts
        ):
            raise WorkDenied("systemic review required before verification")
        self.status(provider, policy, ref, item, "review")
        return self.launch(
            goal_id,
            number,
            request,
            estimated_cost,
            actor=WorkActor("control:queue-execution", policy.authority.coordinator),
            run_role="verify",
            parent_session_id=row["session_id"],
            verified_terminal_parent=True,
        )

    def advance_deferred_completion(self, goal_id, number):
        """Launch one verification run after a bound worker has terminated.

        This is internal queue reconciliation, never a caller-selectable update
        action. A worker can request completion while its own turn is running;
        only this path observes its terminal boundary and verifies its signed
        candidate effect before it creates the separate verification session.
        """
        with self.lock:
            scope, provider, policy, ref = self.scope(goal_id, number)
            configured_github_delivery = scope.github_event_delivery_profiles.get(
                str(number)
            ) == getattr(
                scope.github_acceptance_profiles.get(str(number)), "revision", None
            )
            if (
                scope.candidate_source != "local_file"
                and not configured_github_delivery
            ):
                return None
            item = self.checked(scope, policy, provider.get_item(ref))
            work_links = [
                link
                for link in self.store.links(goal_id=goal_id, number=number)
                if link["run_role"] == "work"
            ]
            if not work_links:
                return None
            row = work_links[-1]
            github_delivery = self._github_event_delivery(scope, number, row["run_key"])
            if scope.candidate_source != "local_file" and not github_delivery:
                return None
            key = identity(row["run_key"], "complete", None)
            events = self.store.events(key)
            if (
                not events
                or any(event["event_kind"] == "outcome" for event in events)
                or any(
                    event["event_kind"] != "intent"
                    or event["action_category"] != "complete"
                    for event in events
                )
            ):
                return None
            session = self.harness.resolve(row["run_key"])
            if session is None:
                return None
            # Only the immutable hub-signed provenance mode may create a
            # separate verifier. Marker-only runs settle through their bound
            # daemon-attested effect and native owner, never this queue path.
            if (
                scope.candidate_source == "local_file"
                and self._signed_committed_provenance(row["run_key"]) is not True
            ):
                return None
            terminal_done = getattr(self.harness, "terminal_work_done", None)
            if not callable(terminal_done) or terminal_done(row["run_key"]) is not True:
                return None
            if scope.candidate_source == "local_file":
                artifact = item.get("expected_effect_artifact")
                materialized = getattr(
                    self.harness, "expected_effect_materialized", None
                )
                if (
                    not isinstance(artifact, str)
                    or not artifact
                    or not callable(materialized)
                    or materialized(row["run_key"], artifact) is not True
                ):
                    return None
            else:
                source = self._verification_criteria(provider, ref, item)
                if (
                    provider.exact_head_check_state(source["head"], source["branch"])
                    != "green"
                ):
                    return None
            verification = self._launch_verification(
                provider,
                policy,
                ref,
                item,
                row,
                goal_id=goal_id,
                number=number,
                estimated_cost=scope.execution_estimated_cost,
            )
            self.store.event(
                "control:queue-execution",
                ref,
                key,
                "outcome",
                "complete",
                outcome="verification_scheduled",
                link_id=row["id"],
            )
            return verification

    def advance_deferred_verification(self, goal_id, number):
        """Accept one bound verifier report after its exact turn succeeds.

        This internal reconciliation is the verifier counterpart to
        :meth:`advance_deferred_completion`: the verifier can report a passed
        check while its structured turn is active, but it cannot self-accept.
        """
        with self.lock:
            scope, provider, policy, ref = self.scope(goal_id, number)
            if scope.candidate_source != "local_file" and not any(
                self._github_event_delivery(scope, number, row["run_key"])
                for row in self.store.links(goal_id=goal_id, number=number)
                if row["run_role"] == "verify"
            ):
                return None
            item = self.checked(scope, policy, provider.get_item(ref))
            links = self.store.links(goal_id=goal_id, number=number)
            verify_links = [link for link in links if link["run_role"] == "verify"]
            if not verify_links:
                return None
            row = verify_links[-1]
            key = identity(row["run_key"], "verified", None)
            events = self.store.events(key)
            if (
                len(events) != 1
                or events[0]["event_kind"] != "intent"
                or events[0]["action_category"] != "verified"
                or not events[0]["provider_operation_id"]
            ):
                return None
            parent = self._work_parent(links=links, row=row)
            if parent is None:
                return None
            github_delivery = self._github_event_delivery(scope, number, row["run_key"])
            if scope.candidate_source == "local_file":
                if self._signed_committed_provenance(parent) is not True:
                    return None
            elif not (
                github_delivery and self._github_event_delivery(scope, number, parent)
            ):
                return None
            terminal_done = getattr(self.harness, "terminal_work_done", None)
            if (
                not callable(terminal_done)
                or terminal_done(row["run_key"]) is not True
                or (github_delivery and terminal_done(parent) is not True)
            ):
                return None
            criteria = self._verification_criteria(provider, ref, item)
            _, expected = verification_identity(
                self.config.workspace_id, goal_id, number, parent, criteria
            )
            if row["run_key"] != expected:
                return None
            if github_delivery:
                facts = provider.acceptance_facts(
                    ref, events[0]["provider_operation_id"]
                )
                if any(facts.get(key) != value for key, value in criteria.items()):
                    return None
                if (
                    provider.acceptance_facts(ref, events[0]["provider_operation_id"])
                    != facts
                ):
                    return None
            provider.close(ref)
            self.store.event(
                "control:queue-execution",
                ref,
                key,
                "outcome",
                "verified",
                outcome="verified",
                # The deferred host report's intent retains only its evidence
                # key. Preserve the full provider-resolved verdict on outcome
                # so a later native-owner receipt cannot reconstruct facts from
                # mutable issue text or a caller-supplied label.
                operation_id=(
                    json.dumps(facts, sort_keys=True)
                    if github_delivery
                    else events[0]["provider_operation_id"]
                ),
                link_id=row["id"],
            )
            if github_delivery:
                self._queue_github_native_owner_wake(
                    scope, provider, ref, parent, row["run_key"], facts
                )
            return {"outcome": "verified"}

    def _github_event_continuation_pending(self, scope, number):
        """Whether a signed GitHub pair still needs its retained CI delivery.

        This is deliberately derived from append-only work intents and signed
        launch bindings.  It does not turn a mutable work-item stage or a
        webhook conclusion into a readiness claim.
        """
        for row in self.store.links(goal_id=scope.goal_id, number=number):
            if row["run_role"] not in {
                "work",
                "verify",
            } or not self._github_event_delivery(scope, number, row["run_key"]):
                continue
            action = "complete" if row["run_role"] == "work" else "verified"
            events = self.store.events(identity(row["run_key"], action, None))
            if any(event["event_kind"] == "outcome" for event in events):
                continue
            # A submitted intent is pending terminal evidence. A separately
            # launched verifier has no report yet, but must retain the exact
            # green source observation until it either reports or is rejected.
            if events or row["run_role"] == "verify":
                return True
        return False

    def advance_github_ci(self, repo, pull_number, head):
        """Continue or retain one exact pushed CI observation.

        ``pending`` deliberately leaves the signed delivery leased for a
        bounded retry.  ``settled`` includes stale, failed, unsupported, and
        already-acknowledged paths; none creates an effect.
        """
        if not isinstance(repo, str) or not isinstance(pull_number, int):
            return "settled"
        pending = False
        with self.lock:
            for scope in self.config.goals:
                for number, profile in scope.github_acceptance_profiles.items():
                    if scope.repo != repo or profile.pull_number != pull_number:
                        continue
                    if (
                        scope.github_event_delivery_profiles.get(number)
                        != profile.revision
                    ):
                        continue
                    try:
                        provider, _, ref, item = self.item(scope.goal_id, int(number))
                        # The coordinator may already have closed the issue
                        # when its post-outcome Factory write was unavailable.
                        # Retain this signed delivery only long enough to
                        # recreate that idempotent wake; acceptance discovery
                        # never runs against a closed item.
                        if item.get("state") == "closed":
                            pending = pending or (
                                self.advance_github_native_owner(scope, provider, ref)
                                == "pending"
                            )
                            continue
                        source = self._verification_criteria(provider, ref, item)
                        if source["head"] != head:
                            continue
                        if (
                            provider.exact_head_check_state(
                                source["head"], source["branch"]
                            )
                            != "green"
                        ):
                            continue
                        self.advance_deferred_completion(scope.goal_id, int(number))
                        self.advance_deferred_verification(scope.goal_id, int(number))
                        pending = pending or (
                            self.advance_github_native_owner(scope, provider, ref)
                            == "pending"
                        )
                        pending = pending or self._github_event_continuation_pending(
                            scope, int(number)
                        )
                    except (PermissionError, ValueError, WorkDenied):
                        continue
        return "pending" if pending else "settled"

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
        row = rows[0]
        scope = next(scope for scope in self.config.goals if scope.goal_id == goal_id)
        existing_binding = self._existing_session_binding(row)
        if (
            existing_binding is not None
            and self._current_existing_session_binding(scope, row, existing_binding)
            is None
        ):
            raise ValueError(
                "existing session binding superseded; new verification required"
            )
        session = self._bound_session(scope, row)
        bound_host_verification = self._bound_host_verification_report(
            scope, row, run_key, session, actor
        )
        if actor.role != policy.authority.coordinator:
            if action == "verified":
                if not bound_host_verification:
                    raise PermissionError("execution reporting credential required")
            elif not actor.host_id or not session or session.host_id != actor.host_id:
                raise PermissionError("execution reporting credential required")
        if evidence_key is not None:
            code(evidence_key)
        key = identity(run_key, action, evidence_key if action == "progress" else None)
        events = self.store.events(key)
        if any(e["event_kind"] == "outcome" for e in events):
            return {"outcome": events[-1]["outcome"]}
        if events:
            if action == "verified":
                recovered = self._recover_observed_github_close(
                    scope, provider, ref, row, key
                )
                if recovered is not None:
                    return recovered
            if action in {"complete", "verified"} and all(
                event["event_kind"] == "intent" and event["action_category"] == action
                for event in events
            ):
                # A bound worker or verifier already requested its terminal
                # successor/acceptance. Repeating that request cannot create a
                # second launch, receipt, or provider action while its turn is active.
                return {"outcome": "deferred"}
            raise WorkDenied("provider operation uncertain; inspection required")
        if item["state"] != "open":
            raise ValueError("item already closed")
        if action == "complete":
            if row["run_role"] != "work":
                raise ValueError("verification needs behavioral result")
            session = self._bound_session(scope, row)
            if session is None:
                raise ValueError("work session must complete before verification")
            # A bound host may request only its own immutable, signed local
            # candidate verifier. Every other completion stays coordinator-only.
            github_delivery = self._github_event_delivery(scope, number, run_key)
            bound_host_completion = (
                scope.candidate_source == "local_file"
                and self._signed_committed_provenance(run_key) is True
            ) or github_delivery
            bound_host_completion = (
                bound_host_completion
                and session.status == "running"
                and not actor.human
                and actor.host_id == session.host_id
            )
            if not bound_host_completion:
                self.coordinator(actor, policy)
            if scope.candidate_source == "local_file":
                # The immutable signed binding, not mutable goal config or a
                # worker body, decides whether this worker can create a verifier.
                provenance = self._signed_committed_provenance(run_key)
                if provenance is False:
                    return {"outcome": "marker_only"}
                if provenance is not True:
                    raise WorkDenied("signed local candidate provenance unavailable")
                # The bound worker cannot select a verification route or quota
                # estimate.  Use the reviewed goal value both for a normal
                # terminal successor and for the active-turn deferred path.
                estimated_cost = scope.execution_estimated_cost
            if session.status != "completed":
                # Only a task-bound local candidate may defer its request. The
                # terminal queue path will re-check this immutable candidate
                # effect before it creates the separate verifier.
                if scope.candidate_source == "local_file" and (
                    not isinstance(item.get("expected_effect_artifact"), str)
                    or not item["expected_effect_artifact"]
                ):
                    raise ValueError("work session must complete before verification")
                if scope.candidate_source != "local_file" and not github_delivery:
                    raise ValueError("work session must complete before verification")
                self.store.event(
                    actor.principal,
                    ref,
                    key,
                    "intent",
                    "complete",
                    link_id=row["id"],
                )
                return {"outcome": "deferred"}
            return self._launch_verification(
                provider,
                policy,
                ref,
                item,
                row,
                goal_id=goal_id,
                number=number,
                estimated_cost=estimated_cost,
            )
        if action not in {"verified", "failed", "progress"}:
            raise ValueError("unsupported update action")
        operation_id = evidence_key if action == "verified" else None
        if action == "verified":
            if row["run_role"] != "verify" or (
                not row["session_id"] and existing_binding is None
            ):
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
            pilot = str(number) in scope.github_acceptance_profiles
            if not pilot and (
                behavioral_passed is not True
                or checks_rerun is not True
                or not evidence_key
            ):
                raise ValueError(
                    "successful behavioral evidence and rerun checks required"
                )
            code(evidence_key)
            criteria = self._verification_criteria(provider, ref, item)
            # Criteria changes invalidate this verification's stable identity.
            _, expected = verification_identity(
                self.config.workspace_id,
                goal_id,
                number,
                self._work_parent(
                    links=self.store.links(goal_id=goal_id, number=number), row=row
                ),
                criteria,
            )
            if run_key != expected:
                raise ValueError(
                    "acceptance criteria changed; new verification required"
                )
            if bound_host_verification:
                # The bound verifier may report its own completed behavioral
                # check, but its running turn cannot close an item. Queue
                # reconciliation rechecks this exact terminal turn before it
                # accepts the report below.
                self.store.event(
                    actor.principal,
                    ref,
                    key,
                    "intent",
                    action,
                    operation_id=evidence_key,
                    link_id=row["id"],
                )
                return {"outcome": "deferred"}
            if session is None and existing_binding is not None:
                raise ValueError(
                    "existing verification session lacks terminal provenance"
                )
            if not self._acceptance_session_is_settled(scope, row, session):
                raise ValueError("verification session must complete before acceptance")
            if pilot:
                parent_key = self._work_parent(
                    links=self.store.links(goal_id=goal_id, number=number), row=row
                )
                parent_row = next(
                    (
                        link
                        for link in self.store.links(goal_id=goal_id, number=number)
                        if link["run_key"] == parent_key
                    ),
                    None,
                )
                parent = (
                    self._bound_session(scope, parent_row)
                    if parent_row is not None
                    else None
                )
                if not self._acceptance_session_is_settled(scope, parent_row, parent):
                    raise ValueError("work session must complete before acceptance")
                facts = provider.acceptance_facts(ref, evidence_key)
                if any(facts.get(k) != v for k, v in criteria.items()):
                    raise ValueError(
                        "acceptance source changed; new verification required"
                    )
                # Repeat trusted reads at the final boundary, before the durable
                # intent. A rejected stale verdict is safe to retry; an uncertain
                # close after intent retains the existing no-second-effect rule.
                if provider.acceptance_facts(ref, evidence_key) != facts:
                    raise ValueError(
                        "acceptance evidence changed; new verification required"
                    )
                # Bounded provider facts, not caller success flags, remain in
                # the existing append-only decision audit alongside evidence ID.
                operation_id = json.dumps(facts, sort_keys=True)
        self.store.event(
            actor.principal,
            ref,
            key,
            "intent",
            action,
            operation_id=operation_id,
            link_id=row["id"],
        )
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
            operation_id=operation_id,
            link_id=row["id"],
        )
        if action == "verified" and scope.candidate_source == "github":
            parent = self._work_parent(
                links=self.store.links(goal_id=goal_id, number=number), row=row
            )
            try:
                facts = json.loads(operation_id)
            except (TypeError, ValueError, json.JSONDecodeError):
                facts = None
            if parent is not None and isinstance(facts, dict):
                self._queue_github_native_owner_wake(
                    scope, provider, ref, parent, row["run_key"], facts
                )
        return {"outcome": action}

    def _work_parent(self, links, row):
        binding = self._existing_session_binding(row)
        if binding is not None:
            parent_run_key = binding.get("parent_run_key")
            if not isinstance(parent_run_key, str):
                return None
            return next(
                (
                    link["run_key"]
                    for link in links
                    if link["run_key"] == parent_run_key and link["run_role"] == "work"
                ),
                None,
            )
        session = self.harness.resolve(row["run_key"])
        return next(
            (
                l["run_key"]
                for l in links
                if session and l["session_id"] == session.parent_session_id
            ),
            None,
        )

    def delegation_allowed(
        self, delegation_id, actor, ref, *, action, stage, operation_key
    ):
        """Consume (idempotently) a single, exact paired-human delegation."""
        if not self.config.audited_delegation.enabled:
            raise PermissionError("audited delegation is disabled")
        if not isinstance(delegation_id, str) or not delegation_id:
            raise PermissionError("delegation required")
        if not self.store.consume_delegation(
            delegation_id=delegation_id,
            delegatee=actor.principal,
            ref=ref,
            action=action,
            stage=stage,
            operation_key=operation_key,
        ):
            raise PermissionError(
                "delegation is invalid, expired, revoked, or replayed"
            )

    def delegate(
        self,
        goal_id,
        number,
        request_key,
        delegatee_principal,
        action,
        stage,
        expires_at,
        *,
        actor,
    ):
        """Create an auditable, one-use launch delegation from a paired device."""
        _, _, ref, _ = self.item(goal_id, number)
        if not self.config.audited_delegation.enabled:
            raise PermissionError("audited delegation is disabled")
        if not actor.paired_human:
            raise PermissionError("authenticated paired device required")
        code(request_key)
        if not isinstance(delegatee_principal, str) or not delegatee_principal:
            raise ValueError("delegatee principal required")
        if not delegatee_principal.startswith("credential:"):
            raise ValueError("credential delegatee required")
        if action != "launch" or stage not in {"work", "verify"}:
            raise ValueError("delegation action and stage not permitted")
        if not isinstance(expires_at, str):
            raise ValueError("delegation expiry required")
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid delegation expiry") from exc
        if expiry.tzinfo is None:
            raise ValueError("delegation expiry timezone required")
        now = datetime.now(timezone.utc)
        lifetime = (expiry - now).total_seconds()
        if not 0 < lifetime <= self.config.audited_delegation.max_lifetime_seconds:
            raise ValueError("delegation expiry outside permitted lifetime")
        return self.store.create_delegation(
            actor=actor.principal,
            ref=ref,
            request_key=identity(
                self.config.workspace_id, goal_id, number, request_key
            ),
            delegatee=delegatee_principal,
            action=action,
            stage=stage,
            expires_at=expiry,
        )

    def revoke_delegation(self, delegation_id, *, actor):
        if not actor.paired_human:
            raise PermissionError("authenticated paired device required")
        if not isinstance(delegation_id, str) or not delegation_id:
            raise ValueError("delegation id required")
        if not self.store.revoke_delegation(delegation_id, actor.principal):
            raise PermissionError("delegation not found or not granted by actor")
        return {"revoked": True}

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
