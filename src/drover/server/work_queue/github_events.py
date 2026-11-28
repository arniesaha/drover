"""Bounded, durable GitHub webhook ingestion for hub-owned work items."""

import hashlib
import hmac
import json
import os
import re
import threading
import time

from .github import ProviderUnavailable

_PR_ACTIONS = {
    "opened": "pr_opened",
    "synchronize": "pr_synchronized",
    "ready_for_review": "pr_ready_for_review",
    "converted_to_draft": "pr_draft",
    "reopened": "pr_reopened",
}
_CI_EVENTS = {"check_suite", "workflow_run", "check_run"}


def _detail(**values):
    """A small, stable JSON detail value suitable for the timeline and cursor."""
    return json.dumps(
        {
            key: value[:256] if isinstance(value, str) else value
            for key, value in values.items()
            if value is not None
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{7,64}", value)
        else None
    )


class GitHubWebhookReceiver:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self._lock = threading.Lock()
        self._health = {
            "received": 0,
            "rejected_bad_signature": 0,
            "deduped": 0,
            "linked": 0,
            "unlinked": 0,
            "processing_errors": 0,
        }

    def health(self):
        with self._lock:
            return dict(self._health)

    def count(self, key, amount=1):
        with self._lock:
            self._health[key] = self._health.get(key, 0) + amount

    def accept(self, headers, body):
        """Verify then persist only; processing always happens off this request."""
        secret = (
            os.environ.get(self.config.secret_env, "") if self.config.secret_env else ""
        )
        signature = headers.get("X-Hub-Signature-256", "")
        if not secret or not signature.startswith("sha256="):
            self.count("rejected_bad_signature")
            return 401, {"error": "invalid webhook signature"}
        expected = (
            "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        )
        if not hmac.compare_digest(signature, expected):
            self.count("rejected_bad_signature")
            return 401, {"error": "invalid webhook signature"}
        delivery_id = headers.get("X-GitHub-Delivery", "")
        event_type = headers.get("X-GitHub-Event", "")
        if (
            not delivery_id
            or len(delivery_id) > 256
            or not event_type
            or len(event_type) > 64
        ):
            self.count("rejected_bad_signature")
            return 401, {"error": "invalid webhook delivery"}
        try:
            payload = body.decode("utf-8")
            if not isinstance(json.loads(payload), dict):
                raise ValueError
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return 400, {"error": "invalid webhook JSON"}
        if not self.store.persist_github_webhook(
            delivery_id=delivery_id, event_type=event_type, payload=payload
        ):
            self.count("deduped")
            return 202, {"accepted": True, "deduped": True}
        self.count("received")
        return 202, {"accepted": True}


class GitHubWebhookWorker:
    """Process persisted delivery rows with a deliberately small polling loop."""

    def __init__(
        self,
        store,
        config,
        receiver,
        providers=None,
        service=None,
        retry_delay_seconds=5,
    ):
        self.store, self.config, self.receiver = store, config, receiver
        self.providers = providers or {}
        self.service = service
        self.retry_delay_seconds = retry_delay_seconds
        self._stop = threading.Event()
        self._thread = None

    def start(self, shutdown_event=None):
        if self._thread is not None:
            return

        def run():
            while not self._stop.is_set() and not (
                shutdown_event and shutdown_event.is_set()
            ):
                self.process_once()
                self._stop.wait(0.25)

        self._thread = threading.Thread(
            target=run, name="github-work-item-events", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def health(self):
        return self.receiver.health()

    def process_once(self):
        rows = self.store.claim_github_webhooks(self.config.batch_size)
        for row in rows:
            error = None
            retry = False
            try:
                retry = self._process(row["event_type"], json.loads(row["payload"]))
            except (AttributeError, KeyError, TypeError, ValueError):
                # Bad signed upstream data is completed and observable. Store
                # failures intentionally escape, retaining the leased row.
                error = "processing_error"
                self.receiver.count("processing_errors")
            if retry:
                self.store.retry_github_webhook(
                    row, delay_seconds=self.retry_delay_seconds
                )
            else:
                self.store.complete_github_webhook(row, error=error)
        return len(rows)

    def _item(self, payload, *, pr_number=None, branch=None):
        repo = (payload.get("repository") or {}).get("full_name")
        item = self.store.find_github_item(
            repo=repo, pr_number=pr_number, branch=branch
        )
        if item is None:
            self.receiver.count("unlinked")
        else:
            self.receiver.count("linked")
        return item

    def _process(self, event_type, payload):
        if event_type == "pull_request":
            self._pull_request(payload)
        elif event_type == "pull_request_review":
            self._review(payload)
        elif event_type in _CI_EVENTS:
            return self._ci(event_type, payload)
        elif event_type == "push":
            self._push(payload)
        elif event_type == "deployment_status":
            self._deployment_status(payload)

    def _pull_request(self, payload):
        pr = payload.get("pull_request") or {}
        number, action = payload.get("number"), payload.get("action")
        head = pr.get("head") or {}
        item = self._item(payload, pr_number=number, branch=head.get("ref"))
        if item is None:
            return
        sha = _sha(head.get("sha"))
        if action == "closed":
            kind = "pr_merged" if pr.get("merged") else "pr_closed"
        else:
            kind = _PR_ACTIONS.get(action)
        if kind:
            self.store.append_github_fact(
                item["id"],
                kind=kind,
                detail=_detail(pr=number, action=action, head_sha=sha),
                head_sha=sha,
            )

    def _review(self, payload):
        if payload.get("action") != "submitted":
            return
        pr, review = payload.get("pull_request") or {}, payload.get("review") or {}
        item = self._item(
            payload,
            pr_number=payload.get("pull_request", {}).get("number"),
            branch=(pr.get("head") or {}).get("ref"),
        )
        if item is not None:
            self.store.append_github_fact(
                item["id"],
                kind="review_submitted",
                detail=_detail(
                    state=review.get("state"),
                    commit_id=review.get("commit_id"),
                ),
            )

    def _ci(self, event_type, payload):
        node = payload.get(event_type) or {}
        if node.get("status") != "completed":
            return
        sha = _sha(node.get("head_sha"))
        branch = node.get("head_branch")
        prs = node.get("pull_requests") or []
        pr_number = prs[0].get("number") if prs and isinstance(prs[0], dict) else None
        item = self._item(payload, pr_number=pr_number, branch=branch)
        if item is None:
            return
        conclusion = node.get("conclusion")
        exact = bool(sha and sha == item.get("head_sha"))
        self.store.append_github_fact(
            item["id"],
            kind="ci_completed",
            detail=_detail(
                head_sha=sha,
                conclusion=conclusion,
                exact_head=exact,
                source=event_type,
            ),
        )
        # A check-run is only a constituent signal.  A completed suite or
        # workflow is the bounded accumulated rollup when no API credential
        # is available; never turn one green check-run into a green PR.
        if event_type == "check_run" or not exact or not isinstance(conclusion, str):
            return
        state = self._github_check_state(item, sha)
        if state == "pending":
            return
        rollup = (
            "ci_green_exact_head"
            if state == "green" or (state is None and conclusion == "success")
            else "ci_failed"
        )
        if self.store.record_github_ci_rollup(item["id"], sha, rollup):
            self.store.append_github_fact(
                item["id"],
                kind=rollup,
                detail=_detail(
                    head_sha=sha,
                    conclusion=conclusion,
                    exact_head=True,
                    source=(
                        "github_api" if state is not None else "accumulated_webhooks"
                    ),
                ),
            )
        if rollup == "ci_green_exact_head":
            return self._continue_exact_ci(item, pr_number, sha)
        return False

    def _continue_exact_ci(self, item, pr_number, sha):
        """Wake only the installed service adapter; provider facts are reread."""
        advance = getattr(self.service, "advance_github_ci", None)
        if callable(advance) and isinstance(pr_number, int):
            return advance(item.get("repo"), pr_number, sha) == "pending"
        return False

    def _github_check_state(self, item, sha):
        """Prefer the configured read credential; ``None`` means clean fallback."""
        for provider in self.providers.values():
            if getattr(getattr(provider, "scope", None), "repo", None) != item.get(
                "repo"
            ):
                continue
            check = getattr(provider, "exact_head_check_state", None)
            if check is None:
                return None
            try:
                return check(sha, item.get("branch"))
            except ProviderUnavailable:
                return None
        return None

    def _deployment_status(self, payload):
        """Project protected-environment status changes into the owning item.

        GitHub may deliver the same deployment state via webhook and the
        bounded sweep.  The store's (item, run, environment) key makes both
        paths idempotent and keeps the webhook free of provider writes.
        """
        deployment = payload.get("deployment") or {}
        status = payload.get("deployment_status") or {}
        raw_state = status.get("state")
        state = {
            "pending": "pending",
            "queued": "pending",
            "in_progress": "pending",
            "success": "approved",
            "approved": "approved",
            "failure": "rejected",
            "rejected": "rejected",
            "inactive": "rejected",
        }.get(raw_state)
        environment = status.get("environment") or deployment.get("environment")
        # A deployment-status delivery is signed but GitHub's top-level
        # payload varies by event producer.  Link it only when it carries the
        # Actions run identity (including a deployment payload extension);
        # never collapse unrelated deployments on branch alone.
        deployment_payload = deployment.get("payload") or {}
        run_id = (
            status.get("workflow_run_id")
            or deployment.get("workflow_run_id")
            or deployment_payload.get("workflow_run_id")
            or (payload.get("workflow_run") or {}).get("id")
        )
        if (
            not isinstance(run_id, int)
            or run_id <= 0
            or not state
            or not isinstance(environment, str)
        ):
            return
        branch = deployment.get("ref") or (payload.get("workflow_run") or {}).get(
            "head_branch"
        )
        item = self._item(payload, branch=branch)
        if item is None:
            return
        run_url = status.get("environment_url") or (
            f"https://github.com/{item['repo']}/actions/runs/{run_id}"
        )
        sender = payload.get("sender") or {}
        reviewer = sender.get("login") if state != "pending" else None
        self.store.observe_environment_approval(
            item["id"],
            run_id=run_id,
            environment=environment,
            run_url=run_url,
            state=state,
            reviewer=reviewer,
        )

    def _push(self, payload):
        repo = payload.get("repository") or {}
        default = repo.get("default_branch")
        if payload.get("ref") != "refs/heads/" + str(default):
            return
        for item in self.store.github_items_for_repo(repo.get("full_name")):
            if item.get("branch") == default:
                continue
            self.receiver.count("linked")
            self.store.append_github_fact(
                item["id"],
                kind="pr_behind",
                detail=_detail(
                    default_branch=default,
                    head_sha=payload.get("after"),
                ),
            )


class GitHubProviderIngestionWorker:
    """Poll current GitHub source facts for explicitly installed exact items.

    This is authenticated GitHub API polling, not webhook ingestion.  It has
    no delivery HMAC, no local delivery row, and no second effect ledger: every
    observation enters :meth:`WorkService.advance_github_ci`, which rereads the
    provider facts before it can advance a deferred pair or native owner wake.
    A restart deliberately rescans the bounded configuration instead of
    advancing a cursor that could skip a retained pending continuation.
    """

    def __init__(self, service, config, *, clock=time.monotonic):
        self.service, self.config, self.clock = service, config, clock
        self._stop = threading.Event()
        self._thread = None
        self._due = {}
        self._failures = {}
        self._lock = threading.Lock()
        self._health = {
            "source": "github_api_polling",
            "cursor": "none; exact configured sources are rescanned",
            "observed": 0,
            "advanced": 0,
            "pending": 0,
            "unavailable": 0,
            "rejected": 0,
            "last_error": None,
        }

    def health(self):
        with self._lock:
            return dict(self._health)

    def _record(self, key, value=1):
        with self._lock:
            self._health[key] = self._health.get(key, 0) + value

    def _error(self, target, error):
        config = self._config()
        failures = self._failures.get(target, 0) + 1
        self._failures[target] = failures
        delay = min(
            config.max_backoff_seconds,
            config.retry_seconds * (2 ** min(failures - 1, 16)),
        )
        self._due[target] = self.clock() + delay
        with self._lock:
            self._health["unavailable"] += 1
            self._health["last_error"] = str(error)[:128]

    def _iteration_error(self, error):
        """Expose a recoverable scan failure without converting it to success."""
        with self._lock:
            self._health["unavailable"] += 1
            self._health["last_error"] = str(error)[:128]

    def _config(self):
        """Read the current validated config so a reload can disable polling."""
        return getattr(self.service.config, "github_provider_ingestion", self.config)

    def _targets(self):
        for scope in sorted(self.service.config.goals, key=lambda row: row.goal_id):
            for raw_number, revision in sorted(
                scope.github_provider_ingestion_profiles.items(),
                key=lambda row: int(row[0]),
            ):
                profile = scope.github_acceptance_profiles.get(raw_number)
                if (
                    profile is None
                    or revision != profile.revision
                    or scope.github_event_delivery_profiles.get(raw_number) != revision
                ):
                    # Configuration validation prevents this at construction;
                    # retain fail-closed behavior if a live object is replaced.
                    continue
                yield scope, int(raw_number), revision

    def start(self, shutdown_event=None):
        if self._thread is not None:
            return

        def run():
            while not self._stop.is_set() and not (
                shutdown_event and shutdown_event.is_set()
            ):
                try:
                    self.process_once()
                except Exception as exc:
                    # Do not catch BaseException: process termination signals
                    # must retain their ordinary lifecycle semantics.
                    self._iteration_error(exc)
                self._stop.wait(self._config().interval_seconds)

        self._thread = threading.Thread(
            target=run, name="github-provider-ingestion", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def process_once(self):
        """Observe at most one configured batch; provider failure is retryable."""
        config = self._config()
        if not config.enabled:
            return 0
        now = self.clock()
        targets = [
            target
            for target in self._targets()
            if self._due.get((target[0].goal_id, target[1]), 0) <= now
        ][: config.batch_size]
        for scope, number, revision in targets:
            target = (scope.goal_id, number)
            try:
                provider, _, ref, item = self.service.item(scope.goal_id, number)
                source = provider.acceptance_source(
                    ref, allow_closed=item.get("state") == "closed"
                )
                if (
                    source.get("repo") != scope.repo
                    or source.get("pull")
                    != scope.github_acceptance_profiles[str(number)].pull_number
                    or source.get("profile") != revision
                    or not _sha(source.get("head"))
                ):
                    raise ValueError("provider source outside installed profile")
                outcome = self.service.advance_github_ci(
                    scope.repo,
                    source["pull"],
                    source["head"],
                    goal_id=scope.goal_id,
                    item_number=number,
                )
            except ProviderUnavailable as exc:
                self._error(target, exc)
                continue
            except (PermissionError, ValueError, KeyError) as exc:
                # Invalid labels, changed criteria, wrong source and revoked
                # scopes are settled failures. They never produce an effect.
                self._due.pop(target, None)
                self._failures.pop(target, None)
                self._record("rejected")
                with self._lock:
                    self._health["last_error"] = str(exc)[:128]
                continue
            except Exception as exc:
                # Store, native-owner and unexpected provider failures must
                # never stop later independent targets. They retain a bounded
                # retry and do not imply an accepted provider observation.
                self._error(target, exc)
                continue
            self._record("observed")
            self._due.pop(target, None)
            self._failures.pop(target, None)
            if outcome == "pending":
                self._due[target] = self.clock() + config.retry_seconds
                self._record("pending")
            else:
                # Avoid repeatedly reading an already-settled/ACKed target at
                # the normal cadence. Restart still performs one safe rescan.
                self._due[target] = self.clock() + config.settled_seconds
                self._record("advanced")
        return len(targets)
