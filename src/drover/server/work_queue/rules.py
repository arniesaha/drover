"""Conservative, idempotent work-item continuity rules."""

from __future__ import annotations

import json
import threading
import time

from .github import ProviderUnavailable


class WorkItemRuleWorker:
    """Evaluate facts once; provider writes happen only after a durable claim."""

    def __init__(self, store, providers, config):
        self.store, self.providers, self.config = store, providers, config
        self.health = {"evaluations": 0, "actions": 0, "skips": 0, "wakes": 0}

    def _record(self, item, kind, detail):
        self.store.append_rule_event(item["id"], kind=kind, detail=detail)

    def _finish(self, item, rule, sha, state, reason=None):
        self.store.complete_rule_action(
            item["id"], rule, sha, state=state, reason=reason
        )
        self._record(item, "rule_" + state, reason or rule)
        self.health["actions" if state in {"acted", "would_act"} else "skips"] += 1

    def _wake(self, item, reason, sha):
        rule = "wake:" + reason
        if self.store.claim_rule_action(item["id"], rule, sha, dry_run=False):
            self.store.complete_rule_action(
                item["id"], rule, sha, state="woke", reason=reason
            )
            self._record(item, "orchestrator_wake", reason)
            self.health["wakes"] += 1

    def _reconcile_environment_approvals(self):
        """Bounded provider-read fallback for missed GitHub deliveries."""
        for repo, provider in self.providers.items():
            fetch = getattr(provider, "list_environment_approvals", None)
            if fetch is None:
                continue
            try:
                approvals = fetch()
            except ProviderUnavailable:
                continue
            for approval in approvals:
                item = self.store.find_github_item(
                    repo=repo, branch=approval.get("branch")
                )
                if item is None:
                    continue
                changed = self.store.observe_environment_approval(
                    item["id"],
                    run_id=approval["run_id"],
                    environment=approval["environment"],
                    run_url=approval["run_url"],
                    state=approval.get("state", "pending"),
                    reviewer=approval.get("reviewer"),
                )
                if changed and approval.get("state") == "approved":
                    self._wake(
                        item, "environment_approved", "run:" + str(approval["run_id"])
                    )
        # A delivery can be missed after GitHub accepts it. Re-read each local
        # pending run on the 30--60 minute sweep and resolve only unambiguous
        # workflow states; unknown stays pending and can escalate once.
        for repo, provider in self.providers.items():
            state_for = getattr(provider, "environment_approval_state", None)
            if state_for is None:
                continue
            for approval in self.store.pending_environment_approvals(repo):
                try:
                    state = state_for(approval["run_id"])
                except ProviderUnavailable:
                    continue
                if state == "pending":
                    continue
                changed = self.store.observe_environment_approval(
                    approval["work_item_id"],
                    run_id=approval["run_id"],
                    environment=approval["environment"],
                    run_url=approval["run_url"],
                    state=state,
                    reviewer=None,
                )
                if changed and state == "approved":
                    item = self.store.hub_item(approval["work_item_id"])
                    if item is not None:
                        self._wake(
                            item,
                            "environment_approved",
                            "run:" + str(approval["run_id"]),
                        )
        policy = getattr(self.config, "github_approvals", None)
        threshold = getattr(policy, "stale_escalation_seconds", 14_400)
        self.store.pending_environment_approval_escalations(threshold)

    def evaluate(self, item, kind, sha):
        self.health["evaluations"] += 1
        sha = sha or "event:" + kind
        provider = self.providers.get(item.get("repo"))
        if kind == "environment_approval_approved":
            return self._wake(item, "environment_approved", sha)
        if kind == "environment_approval_rejected":
            return None  # terminal: only a human may create a replacement work item
        if kind == "worker_done" and item.get("publish") and not item.get("pr"):
            if provider is None:
                return self._wake(item, "github_credential_unavailable", sha)
            if not self.store.claim_rule_action(
                item["id"], "publish", sha, dry_run=self.config.dry_run
            ):
                return
            try:
                remote = provider.branch_head(item.get("branch"))
            except ProviderUnavailable:
                return self._wake(item, "github_credential_unavailable", sha)
            if remote is None:
                return self._finish(
                    item, "publish", sha, "skipped", "branch_not_pushed"
                ) or self._wake(item, "branch_not_pushed", sha)
            if remote != sha:
                return self._finish(
                    item, "publish", sha, "skipped", "head_mismatch"
                ) or self._wake(item, "head_mismatch", sha)
            if self.config.dry_run:
                return self._finish(item, "publish", sha, "would_act")
            try:
                pr = provider.open_draft_pr(item["branch"], item["title"])
            except ProviderUnavailable:
                return self._wake(item, "github_credential_unavailable", sha)
            self.store.patch_hub_item(item["id"], pr=pr)
            return self._finish(item, "publish", sha, "acted")
        if kind == "ci_green_exact_head":
            if item.get("head_sha") != sha:
                return self._record(item, "rule_skipped", "stale_head")
            if not item.get("auto_merge") or item.get("automation_gate") != "none":
                return self._record(item, "rule_skipped", "policy_gate")
            if provider is None or not item.get("pr"):
                return self._wake(item, "github_credential_unavailable", sha)
            if not self.store.claim_rule_action(
                item["id"], "merge", sha, dry_run=self.config.dry_run
            ):
                return
            try:
                if not provider.pull_is_current(item["pr"]):
                    return self._finish(item, "merge", sha, "skipped", "pr_behind")
                if self.config.dry_run:
                    return self._finish(item, "merge", sha, "would_act")
                provider.mark_pr_ready(item["pr"])
                provider.squash_merge(item["pr"], sha)
            except ProviderUnavailable:
                return self._wake(item, "github_credential_unavailable", sha)
            return self._finish(item, "merge", sha, "acted")
        if kind == "pr_behind" and item.get("pr"):
            action_sha = item.get("head_sha") or sha
            if not (item.get("publish") or item.get("auto_merge")):
                return self._record(item, "rule_skipped", "policy_disabled")
            if self.store.has_timeline_kind(item["id"], "ci_failed"):
                return self._wake(item, "ci_failed", action_sha)
            if provider is None:
                return self._wake(item, "github_credential_unavailable", action_sha)
            if not self.store.claim_rule_action(
                item["id"], "update_branch", action_sha, dry_run=self.config.dry_run
            ):
                return
            if self.config.dry_run:
                return self._finish(item, "update_branch", action_sha, "would_act")
            try:
                provider.update_pr_branch(item["pr"], action_sha)
            except ProviderUnavailable:
                return self._wake(item, "github_credential_unavailable", action_sha)
            return self._finish(item, "update_branch", action_sha, "acted")
        if kind == "pr_merged":
            if self.store.claim_rule_action(
                item["id"], "done", sha, dry_run=self.config.dry_run
            ):
                if self.config.dry_run:
                    return self._finish(item, "done", sha, "would_act")
                self.store.mark_done_and_unblock(item["id"])
                return self._finish(item, "done", sha, "acted")
        if kind in {
            "ci_failed",
            "needs_input",
            "worker_error",
            "blocked",
            "review_submitted",
        }:
            self._wake(item, kind, sha)

    def process_once(self):
        cursor = self.store.rule_cursor()
        page = self.store.work_item_events(since=cursor, limit=self.config.batch_size)
        for event in page["events"]:
            if event["kind"].startswith(("rule_", "orchestrator_wake")):
                self.store.advance_rule_cursor(event["cursor"])
                continue
            item = self.store.hub_item(event["item_id"])
            if item is not None:
                detail = event.get("detail")
                if isinstance(detail, str) and detail.startswith("{"):
                    try:
                        detail = json.loads(detail).get("head_sha")
                    except (TypeError, ValueError):
                        detail = None
                self.evaluate(item, event["kind"], detail)
            self.store.advance_rule_cursor(event["cursor"])
        return len(page["events"])

    def start(self, shutdown_event=None):
        if hasattr(self, "_thread"):
            return
        stop = threading.Event()
        self._stop = stop

        def run():
            last_sweep = 0.0
            last_approval_sweep = 0.0
            while not stop.is_set() and not (
                shutdown_event and shutdown_event.is_set()
            ):
                self.process_once()
                now = time.monotonic()
                if now - last_sweep >= self.config.sweep_seconds:
                    for item in self.store.hub_items(state="open"):
                        if (
                            item.get("publish")
                            and item.get("head_sha")
                            and not item.get("pr")
                        ):
                            self.evaluate(item, "worker_done", item["head_sha"])
                    last_sweep = now
                policy = getattr(self.config, "github_approvals", None)
                approval_interval = getattr(policy, "reconciliation_seconds", 1800)
                if now - last_approval_sweep >= approval_interval:
                    self._reconcile_environment_approvals()
                    last_approval_sweep = now
                stop.wait(0.25)

        self._thread = threading.Thread(target=run, name="work-item-rules", daemon=True)
        self._thread.start()

    def stop(self):
        if hasattr(self, "_stop"):
            self._stop.set()
            self._thread.join(timeout=2)
            del self._thread
