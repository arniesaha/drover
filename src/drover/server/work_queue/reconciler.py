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
from .github import GitHubProvider, ProviderUnavailable
from .service import WorkActor, identity

log = logging.getLogger(__name__)


class QueueExecutionCoordinator:
    """The sole automatic path delegates to the existing WorkService launcher."""

    def __init__(self, service):
        self.service = service
        self.health = {"launches": 0, "cap_denials": 0, "budget_denials": 0}

    def launch_candidate(self, goal_id, number):
        with self.service.lock:
            config = self.service.config
            scope = next(
                (item for item in config.goals if item.goal_id == goal_id), None
            )
            if scope is None or not scope.execution_enabled or config.shadow_mode:
                return {"denied": "execution_disabled"}
            if self.service.execution_kill_switch or config.execution.kill_switch:
                return {"denied": "kill_switch"}
            active = self.service.harness.active(self.service.store.links())
            if len(active) >= config.execution.global_max_workers or (
                sum(row["goal_id"] == goal_id for row in active)
                >= (scope.execution_max_workers or config.execution.global_max_workers)
            ):
                self.health["cap_denials"] += 1
                return {"denied": "global_cap"}
            if (
                scope.execution_budget is not None
                and scope.execution_estimated_cost > scope.execution_budget
            ):
                self.health["budget_denials"] += 1
                return {"denied": "budget"}
            actor = WorkActor("control:queue-execution", "nix")
            row = self.service.launch(
                goal_id,
                number,
                identity("automatic-launch", goal_id, number),
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
            service.config, service_principal="control:queue-execution"
        )
        self._stop = threading.Event()
        self._thread = None

    def process_once(self):
        report = self.reconciler.poll()
        for candidate in report["actions"]:
            if candidate["kind"] == "launch_candidate":
                try:
                    self.coordinator.launch_candidate(
                        candidate["goal_id"], candidate["number"]
                    )
                except Exception:
                    # A malformed candidate or failed launch must not stop the daemon.
                    log.exception("work queue launch candidate failed")

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
        provider_factory=GitHubProvider,
        clock=time.time,
    ):
        if not service_principal.strip():
            raise ValueError("authenticated control principal required")
        self.service_principal = service_principal
        self.config = config
        self.clock = clock
        self.providers = {
            g.goal_id: provider_factory(config.workspace_id, g) for g in config.goals
        }
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
