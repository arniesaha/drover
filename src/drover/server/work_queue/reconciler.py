"""Single-process level-triggered polling, producing a read-only report."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone

from .config import WorkQueueConfig, load_policy
from .github import ProviderUnavailable
from .providers import provider_for_scope
from .service import WorkActor, WorkDenied, identity

log = logging.getLogger(__name__)


class QueueExecutionCoordinator:
    """The sole automatic path delegates to the existing WorkService launcher."""

    def __init__(self, service):
        self.service = service
        self.health = {
            "launches": 0,
            "cap_denials": 0,
            "budget_denials": 0,
            "execution_disabled_denials": 0,
            "kill_switch_denials": 0,
        }

    def _denied(self, reason, goal_id, number):
        self.health[
            {"global_cap": "cap_denials", "budget": "budget_denials"}.get(
                reason, reason + "_denials"
            )
        ] += 1
        log.warning(
            "work queue launch denied reason=%s goal_id=%s issue_number=%s",
            reason,
            goal_id,
            number,
        )
        return {"denied": reason}

    def launch_candidate(self, goal_id, number):
        with self.service.lock:
            config = self.service.config
            scope = next(
                (item for item in config.goals if item.goal_id == goal_id), None
            )
            if scope is None or not scope.execution_enabled or config.shadow_mode:
                return self._denied("execution_disabled", goal_id, number)
            if self.service.execution_kill_switch or config.execution.kill_switch:
                return self._denied("kill_switch", goal_id, number)
            active = self.service.harness.active(self.service.store.links())
            if len(active) >= config.execution.global_max_workers or (
                sum(row["goal_id"] == goal_id for row in active)
                >= (scope.execution_max_workers or config.execution.global_max_workers)
            ):
                return self._denied("global_cap", goal_id, number)
            if (
                scope.execution_budget is not None
                and scope.execution_estimated_cost > scope.execution_budget
            ):
                return self._denied("budget", goal_id, number)
            # A terminal worker is durable evidence of a completed/aborted
            # attempt, not an idempotency target for the next queued attempt.
            # The stable automatic key is intentionally reused while its
            # session is live, but a new client id is required after terminal
            # state: harnessd correctly deduplicates client ids forever.
            request_key = identity("automatic-launch", goal_id, number)
            for link in reversed(
                self.service.store.links(goal_id=goal_id, number=number)
            ):
                session = (
                    self.service.harness.resolve(link["run_key"])
                    if link["session_id"]
                    else None
                )
                if (
                    link["run_role"] == "work"
                    and session is not None
                    and session.status
                    in {"completed", "failed", "errored", "terminated", "cancelled"}
                ):
                    request_key = identity(
                        "automatic-relaunch",
                        goal_id,
                        number,
                        link["run_key"],
                        session.session_id,
                    )
                    break
            actor = WorkActor("control:queue-execution", "nix")
            row = self.service.launch(
                goal_id,
                number,
                request_key,
                scope.execution_estimated_cost,
                actor=actor,
            )
        self.health["launches"] += 1
        return {"launched": True, "link": row}


class QueueExecutionWorker:
    """Consumes the established candidate projection; it does not launch directly."""

    def __init__(self, service):
        self.service = service
        self.coordinator = QueueExecutionCoordinator(service)
        self.reconciler = ShadowReconciler(
            service.config,
            service_principal="control:queue-execution",
            providers=getattr(service, "providers", None),
        )
        self._stop = threading.Event()
        self._thread = None
        # Automatic launch failures are deliberately contained so one bad
        # candidate cannot stop the queue.  Retain only a bounded, sanitized
        # per-candidate category for the device-authenticated status report.
        self._last_launch_failures = {}

    @staticmethod
    def _failure_category(exc):
        if isinstance(exc, WorkDenied):
            if str(exc) == "quota or capability unavailable":
                return "quota_or_capability_unavailable"
            if str(exc) == "launch intent uncertain; inspection required":
                return "launch_intent_uncertain"
            return "work_denied"
        return "launch_error"

    def report(self):
        report = self.reconciler.report()
        execution = dict(self.coordinator.health)
        # A rollout must wait for an already-accepted launch as well as for
        # candidates still waiting in the reconciler.  This is intentionally
        # derived from the same durable links that enforce the capacity cap.
        try:
            execution["active"] = len(
                self.service.harness.active(self.service.store.links())
            )
        except Exception:  # unknown is unsafe for a rollout, not zero
            execution["active"] = None
        execution["recent_launch_failures"] = [
            dict(failure)
            for failure in sorted(
                self._last_launch_failures.values(),
                key=lambda failure: (
                    str(failure["goal_id"] or ""),
                    failure["item_number"] or -1,
                ),
            )
        ]
        report["execution"] = execution
        return report

    def _recover_typed_worker_auth_failure(self, candidate):
        """Requeue one terminal local worker after a typed auth failure.

        This is not a general retry for a worker-declared block.  It requires
        the exact structured daemon category, the terminal session bound to
        the current durable work link, and a negative expected-effect
        readback.  An append-only intent/outcome pair fences the recovery to
        one automatic requeue for that source run.  Human gates, prose-only
        blocks, unavailable readbacks, and already-materialized effects stay
        fail-closed.
        """
        goal_id = candidate.get("goal_id")
        number = candidate.get("number")
        scope = next(
            (goal for goal in self.service.config.goals if goal.goal_id == goal_id),
            None,
        )
        if (
            scope is None
            or scope.candidate_source != "local_file"
            or not scope.continuity_owner_id
        ):
            return False
        try:
            provider, policy, ref, item = self.service.item(goal_id, number)
        except (KeyError, PermissionError, ValueError):
            return False
        statuses = {
            label["name"]
            for label in item.get("labels", [])
            if isinstance(label, dict) and isinstance(label.get("name"), str)
        }
        if policy.labels.status_prefix + "running" not in statuses:
            return False
        artifact = item.get("expected_effect_artifact")
        if not isinstance(artifact, str) or not artifact:
            return False
        links = self.service.store.links(goal_id=goal_id, number=number)
        work_links = [link for link in links if link.get("run_role") == "work"]
        if not work_links:
            return False
        link = work_links[-1]
        run_key = link["run_key"]
        if self.service.harness.coordinator_failure_category(run_key) != (
            "authentication_required"
        ):
            return False
        # A positive result is durable candidate evidence and must never be
        # retried into a second possible effect.  Unknown readback is equally
        # unsafe to retry.
        if (
            self.service.harness.expected_effect_materialized(run_key, artifact)
            is not False
        ):
            return False
        key = identity("automatic-work-coordinator-auth-recovery", run_key)
        events = self.service.store.events(key)
        if events:
            return False
        actor = WorkActor("control:queue-execution", "nix")
        self.service.store.event(
            actor.principal,
            ref,
            key,
            "intent",
            "recover_authenticated_worker",
            link_id=link["id"],
        )
        try:
            self.service.status(provider, policy, ref, item, "queued")
        except Exception:
            # The durable intent makes an uncertain provider write
            # inspection-required rather than repeatedly relaunching work.
            return False
        self.service.store.event(
            actor.principal,
            ref,
            key,
            "outcome",
            "recover_authenticated_worker",
            outcome="queued",
            link_id=link["id"],
        )
        return True

    def process_once(self):
        report = self.reconciler.poll()
        for candidate in report["actions"]:
            if candidate["kind"] == "launch_candidate":
                try:
                    self.coordinator.launch_candidate(
                        candidate["goal_id"], candidate["number"]
                    )
                except Exception as exc:
                    # A malformed candidate or failed launch must not stop the daemon.
                    key = (candidate.get("goal_id"), candidate.get("number"))
                    self._last_launch_failures[key] = {
                        "goal_id": candidate.get("goal_id"),
                        "item_number": candidate.get("number"),
                        "category": self._failure_category(exc),
                    }
                    log.exception("work queue launch candidate failed")
            elif candidate["kind"] == "inspect_execution":
                try:
                    self._recover_typed_worker_auth_failure(candidate)
                except Exception:
                    # Recovery is intentionally best-effort and fenced by a
                    # durable intent. A bad status read must not stop other
                    # ready candidates or become a generic retry.
                    log.exception("work queue typed worker recovery failed")

    def start(self, shutdown_event=None):
        if self._thread is not None:
            return

        def run():
            while not self._stop.is_set() and not (
                shutdown_event and shutdown_event.is_set()
            ):
                self.process_once()
                if self._stop.wait(5):
                    return

        self._thread = threading.Thread(
            target=run, name="work-queue-execution", daemon=True
        )
        self._thread.start()


def _identity(workspace, repo, source, action):
    return hashlib.sha256(
        json.dumps([workspace, repo, source, action]).encode()
    ).hexdigest()


class ShadowReconciler:
    def __init__(
        self,
        config: WorkQueueConfig,
        *,
        service_principal: str,
        provider_factory=provider_for_scope,
        providers=None,
        clock=time.time,
    ):
        if not service_principal.strip():
            raise ValueError("authenticated control principal required")
        self.service_principal = service_principal
        self.config = config
        self.clock = clock
        if providers is None:
            providers = {
                g.goal_id: provider_factory(config.workspace_id, g)
                for g in config.goals
            }
        if set(providers) != {goal.goal_id for goal in config.goals}:
            raise ValueError("candidate providers must match configured goals")
        self.providers = providers
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._reports = {}
        self._seen = set()
        self._active_ids = {}

    def poll(self):
        """Replace projections, never append actions or call provider mutations."""
        with self._poll_lock:
            current = set()
            reports = {}
            for scope in self.config.goals:
                provider = self.providers[scope.goal_id]
                previous = self._reports.get(scope.goal_id, {})
                report = {
                    "workspace_id": self.config.workspace_id,
                    "actor_principal": self.service_principal,
                    "goal_id": scope.goal_id,
                    "repo": scope.repo,
                    "shadow_mode": True,
                    "actions": [],
                    "diagnostics": [],
                    "last_success_at": previous.get("last_success_at"),
                    "stale": True,
                }
                try:
                    policy = load_policy(scope.policy_path)
                    provider.authenticate()
                    report["actor_principal"] = provider.actor_principal
                    items = provider.list_items()
                    attention = provider.list_attention()
                    report["policy_version"] = policy.version
                    report["freshness_seconds"] = policy.poll.freshness_seconds
                    for capability in ("pulls_read", "checks_read", "actions_read"):
                        if capability not in provider.capabilities:
                            report["diagnostics"].append(
                                self._row(
                                    report,
                                    "provider",
                                    "permission_unavailable:" + capability,
                                )
                            )
                    for item in items:
                        if item["state"] not in {"open", "closed"}:
                            raise ProviderUnavailable("github_invalid_issue_state")
                        if item["state"] == "closed":
                            continue
                        names = {label["name"] for label in item["labels"]}
                        labels = policy.labels
                        statuses = {
                            n[len(labels.status_prefix) :]
                            for n in names
                            if n.startswith(labels.status_prefix)
                        }
                        agents = {
                            n[len(labels.agent_prefix) :]
                            for n in names
                            if n.startswith(labels.agent_prefix)
                        }
                        priorities = {
                            n[len(labels.priority_prefix) :]
                            for n in names
                            if n.startswith(labels.priority_prefix)
                        }
                        goals = {n for n in names if n.startswith(labels.goal_prefix)}
                        problems = []
                        if len(statuses) != 1 or not statuses <= set(labels.statuses):
                            problems.append("invalid_status_labels")
                        if len(agents) > 1 or not agents <= set(labels.agents):
                            problems.append("invalid_agent_labels")
                        if len(priorities) > 1 or not priorities <= set(
                            labels.priorities
                        ):
                            problems.append("invalid_priority_labels")
                        if len(goals) > 1:
                            problems.append("conflicting_goal_labels")
                        if problems:
                            for problem in problems:
                                report["diagnostics"].append(
                                    self._row(
                                        report, f"issue:{item['number']}", problem
                                    )
                                )
                            continue
                        state = next(iter(statuses))
                        if state == "blocked":
                            continue
                        action = {
                            "queued": "launch_candidate",
                            "running": "inspect_execution",
                            "review": "verify_candidate",
                        }[state]
                        source = f"issue:{item['number']}"
                        if state == "review":
                            # Criteria content is hashed, never included in the report.
                            source += (
                                ":"
                                + hashlib.sha256(
                                    (item.get("body") or "").encode()
                                ).hexdigest()
                            )
                        row = self._row(report, source, action)
                        row.update(
                            {
                                "goal_id": scope.goal_id,
                                "number": item["number"],
                                "url": f"https://github.com/{scope.repo}/issues/{item['number']}",
                                "priority": next(
                                    iter(priorities), policy.priority.default
                                ),
                                "created_at": item.get("created_at"),
                                "execution_allowed": False,
                            }
                        )
                        report["actions"].append(row)
                    for entry in attention:
                        row = self._row(report, entry["source_key"], entry["kind"])
                        row.update(
                            {
                                "url": entry["url"],
                                "source_updated_at": entry["updated_at"],
                                "execution_allowed": False,
                            }
                        )
                        report["actions"].append(row)
                    # Overlapping provider results remain one proposed action.
                    report["actions"] = list(
                        {r["id"]: r for r in report["actions"]}.values()
                    )
                    report["last_success_at"] = self.clock()
                    report["stale"] = False
                except (
                    ProviderUnavailable,
                    ValueError,
                    OSError,
                    KeyError,
                    TypeError,
                    AttributeError,
                ):
                    # Do not reflect exception text or partially read provider content.
                    current.update(self._active_ids.get(scope.goal_id, set()))
                    report["actions"] = []
                    report["diagnostics"] = [
                        self._row(report, "provider", "refresh_failed")
                    ]
                if not report["stale"]:
                    self._active_ids[scope.goal_id] = {
                        r["id"] for r in report["actions"]
                    }
                current.update(self._active_ids.get(scope.goal_id, set()))
                reports[scope.goal_id] = report
            newly_actionable = current - self._seen
            self._seen = current
            with self._lock:
                self._reports = reports
                result = self._snapshot()
            result["new_action_ids"] = sorted(newly_actionable)
            return result

    def _row(self, report, source, kind):
        return {
            "id": _identity(report["workspace_id"], report["repo"], source, kind),
            "workspace_id": report["workspace_id"],
            "actor_principal": report["actor_principal"],
            "source_key": source,
            "kind": kind,
            "observed_at": datetime.fromtimestamp(
                self.clock(), timezone.utc
            ).isoformat(),
        }

    def _snapshot(self):
        now = self.clock()
        goals = copy.deepcopy(list(self._reports.values()))
        actions = {}
        for goal in goals:
            last = goal["last_success_at"]
            age = max(0.0, now - last) if last is not None else None
            goal["refresh_age_seconds"] = age
            goal["refreshed_at"] = (
                datetime.fromtimestamp(last, timezone.utc).isoformat() if last else None
            )
            goal["stale"] = (
                goal["stale"] or age is None or age > goal.get("freshness_seconds", 0)
            )
            for row in goal["diagnostics"]:
                row["refresh_age_seconds"] = age
                row["stale"] = goal["stale"]
            for row in goal["actions"]:
                row["refresh_age_seconds"] = age
                row["stale"] = goal["stale"]
                actions[row["id"]] = row
        return {
            "workspace_id": self.config.workspace_id,
            "shadow_mode": True,
            "goals": goals,
            "actions": list(actions.values()),
            "actionable_count": len(actions),
        }

    def report(self):
        with self._lock:
            return self._snapshot()

    def run(self, stop):
        while not stop.is_set():
            self.poll()
            intervals = []
            for goal in self.config.goals:
                try:
                    intervals.append(
                        load_policy(goal.policy_path).poll.interval_seconds
                    )
                except (ValueError, OSError):
                    intervals.append(load_policy().poll.interval_seconds)
            if stop.wait(min(intervals)):
                return


def start_shadow_reconciler(collector, config, stop, *, auth=None):
    if not config.enabled or not config.shadow_mode:
        return None
    if auth is None or not auth.enabled or auth.credentials is None:
        raise ValueError("enabled work queue requires control credentials")
    service = ShadowReconciler(
        config, service_principal="control:server:" + auth.credentials.server_id
    )
    collector.work_queue = service
    thread = threading.Thread(
        target=service.run, args=(stop,), name="work-queue-shadow", daemon=True
    )
    thread.start()
    return service
