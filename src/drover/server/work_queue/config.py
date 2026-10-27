"""Versioned goal policy and workspace-scoped provider configuration."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Labels(Model):
    statuses: list[str]
    agents: list[str]
    status_prefix: str
    agent_prefix: str
    goal_prefix: str
    priorities: list[str]
    priority_prefix: str

    @model_validator(mode="after")
    def valid(self):
        if set(self.statuses) != {"queued", "running", "review", "blocked"}:
            raise ValueError("policy requires the four queue states")
        prefixes = [
            self.status_prefix,
            self.agent_prefix,
            self.goal_prefix,
            self.priority_prefix,
        ]
        if len(set(prefixes)) != len(prefixes) or any(
            not p.endswith(":") for p in prefixes
        ):
            raise ValueError("label prefixes must be distinct namespaces")
        if not self.agents or len(set(self.agents)) != len(self.agents):
            raise ValueError("agents must be unique and nonempty")
        return self


class Authority(Model):
    coordinator: str
    allowed_actions: list[str]
    human_actions: list[str]
    ambiguous_requires_decision: bool


class Acceptance(Model):
    criteria_owner: str
    amendment_record_required: bool
    separate_verification_run: bool
    human_acceptance_required: bool
    separate_verifier_credential: bool
    behavioral_evidence_required: bool
    rerun_relevant_checks: bool


class Priority(Model):
    default: str
    order: list[str]


class Retries(Model):
    attempts: int = Field(gt=0)
    systemic_review_required: bool
    new_attempt_requires_changed_approach: bool
    new_attempt_authorizer: str
    human_rejection_consumes_attempt: bool


class Capacity(Model):
    goal_workers: int = Field(gt=0)
    workspace_workers: int = Field(gt=0)
    include_verification: bool


class Quota(Model):
    reserve_fraction: float = Field(ge=0, lt=1)
    unknown_pauses_launch: bool
    estimated_cost_required: bool


class Progress(Model):
    stale_seconds: int = Field(gt=0)
    active_tool_seconds: int = Field(gt=0)
    stale_restarts_worker: bool
    explicit_slow_tool_extension: bool


class Window(Model):
    running_may_finish: bool
    extension_limit: int = Field(ge=0)
    extension_seconds: int = Field(gt=0)
    extension_authorizer: str
    unchanged_scope_quota_required: bool
    expired_launch_requires_extension: bool


class Poll(Model):
    interval_seconds: int = Field(gt=0)
    freshness_seconds: int = Field(gt=0)


class Retention(Model):
    links_after_session_days: int = Field(ge=0)
    decision_days: int = Field(gt=0)


class GoalPolicy(Model):
    version: Literal[1]
    labels: Labels
    authority: Authority
    acceptance: Acceptance
    priority: Priority
    retries: Retries
    capacity: Capacity
    quota: Quota
    progress: Progress
    window: Window
    poll: Poll
    retention: Retention

    @model_validator(mode="after")
    def valid(self):
        if self.priority.default not in self.labels.priorities:
            raise ValueError("default priority must be a configured label value")
        return self


def load_policy(path: str = "") -> GoalPolicy:
    source = Path(path) if path else files(__package__).joinpath("policy-v1.json")
    return GoalPolicy.model_validate_json(source.read_text(encoding="utf-8"))


class GitHubCredential(Model):
    mode: Literal["token", "app"] = "token"
    token_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    installation_id: int | None = Field(default=None, gt=0)
    repository_id: int | None = Field(default=None, gt=0)
    capabilities: list[
        Literal[
            "issues_read",
            "pulls_read",
            "checks_read",
            "actions_read",
            "issues_write",
            "pulls_write",
            "contents_read",
        ]
    ]

    @model_validator(mode="after")
    def valid(self):
        if self.mode == "app" and (not self.installation_id or not self.repository_id):
            raise ValueError(
                "App credentials require installation and repository identity"
            )
        return self


class GoalScope(Model):
    goal_id: str = Field(min_length=1)
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    goal_label: str | None = None
    milestone: int | None = Field(default=None, gt=0)
    policy_path: str = ""
    execution_cwds: dict[str, str] = Field(default_factory=dict)
    brief_ref: str | None = None
    execution_enabled: bool = False
    execution_budget: float | None = Field(default=None, gt=0)
    execution_estimated_cost: float = Field(default=5.0, gt=0)
    execution_max_workers: int | None = Field(default=None, gt=0)
    # Explicit opt-in: the one OpenClaw session allowed to receive durable
    # work-completion wakes for this goal.  No default owner exists.
    continuity_owner_id: str | None = None
    # Native owners use the normal structured harness launch contract.  The
    # harness is deliberately explicit: a native owner must not inherit a
    # provider/model default from an unrelated worker.
    continuity_owner_harness: str | None = None
    continuity_owner_model: str | None = None
    credential: GitHubCredential

    @model_validator(mode="after")
    def valid(self):
        if any(part in {".", ".."} for part in self.repo.split("/")):
            raise ValueError("repository traversal is forbidden")
        if bool(self.goal_label) == bool(self.milestone):
            raise ValueError("scope requires exactly one goal label or milestone")
        policy = load_policy(self.policy_path)
        if self.goal_label and not self.goal_label.startswith(
            policy.labels.goal_prefix
        ):
            raise ValueError("goal label must use the policy goal namespace")
        owner = self.continuity_owner_id
        native_owner = "drover:owner/" + self.goal_id
        if owner is not None and len(owner) > 191:
            raise ValueError("continuity owner exceeds the control-plane limit")
        if owner is not None and not (
            owner.startswith("agent:") or owner == native_owner
        ):
            raise ValueError(
                "continuity owner must be an agent key or this goal's native owner"
            )
        if owner and owner.startswith("drover:"):
            if not (
                self.execution_enabled
                and self.execution_cwds
                and self.brief_ref
                and self.continuity_owner_harness
            ):
                raise ValueError(
                    "native continuity owner requires an enabled harness profile"
                )
        return self


class WorkItemEventsConfig(Model):
    """Bounded delivery policy for hub-owned work-item notifications."""

    webhook_allow_hosts: list[str] = Field(default_factory=list)
    max_in_flight: int = Field(default=4, ge=1, le=16)
    batch_size: int = Field(default=16, ge=1, le=100)
    max_attempts: int = Field(default=8, ge=1, le=20)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    @model_validator(mode="after")
    def valid(self):
        normalized = [
            host.strip().lower().rstrip(".") for host in self.webhook_allow_hosts
        ]
        if any(not host or "://" in host or "/" in host for host in normalized):
            raise ValueError("webhook_allow_hosts must contain host names only")
        if len(set(normalized)) != len(normalized):
            raise ValueError("webhook_allow_hosts must not contain duplicates")
        return self


class GitHubWebhookConfig(Model):
    """Ingress limits and the secret reference for GitHub delivery facts."""

    secret_env: str = ""
    max_body_bytes: int = Field(default=128 * 1024, ge=1024, le=1024 * 1024)
    batch_size: int = Field(default=16, ge=1, le=100)

    @model_validator(mode="after")
    def valid(self):
        import re

        if self.secret_env and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.secret_env
        ):
            raise ValueError("github webhook secret_env must be an environment key")
        return self


class WorkItemRulesConfig(Model):
    enabled: bool = False
    dry_run: bool = False
    sweep_seconds: int = Field(default=1800, ge=1800, le=3600)
    batch_size: int = Field(default=32, ge=1, le=100)


class GitHubApprovalConfig(Model):
    """Bounded read-only reconciliation for protected environments."""

    reconciliation_seconds: int = Field(default=1800, ge=1800, le=3600)
    stale_escalation_seconds: int = Field(default=14_400, ge=1800)


class QueueExecutionConfig(Model):
    global_max_workers: int = Field(default=1, ge=1)
    # Awaiting-input workers remain resumable, but cannot reserve capacity forever.
    idle_awaiting_input_minutes: int = Field(default=30, ge=1, le=10_080)
    kill_switch: bool = False


class WorkQueueConfig(Model):
    enabled: bool = False
    shadow_mode: bool = True
    workspace_id: str = ""
    goals: list[GoalScope] = Field(default_factory=list)
    principal_roles: dict[str, str] = Field(default_factory=dict)
    work_item_events: WorkItemEventsConfig = Field(default_factory=WorkItemEventsConfig)
    github_webhook: GitHubWebhookConfig = Field(default_factory=GitHubWebhookConfig)
    rules: WorkItemRulesConfig = Field(default_factory=WorkItemRulesConfig)
    github_approvals: GitHubApprovalConfig = Field(default_factory=GitHubApprovalConfig)
    execution: QueueExecutionConfig = Field(default_factory=QueueExecutionConfig)
    # A bound worker idle at awaiting:input with output but no declared state
    # wakes its owner after this many seconds (continuity owner goals only).
    idle_undeclared_seconds: int = Field(default=300, ge=30, le=86400)

    @model_validator(mode="after")
    def valid(self):
        if self.enabled and (not self.workspace_id.strip() or not self.goals):
            raise ValueError("enabled work queue requires workspace and goals")
        if len({g.goal_id for g in self.goals}) != len(self.goals):
            raise ValueError("duplicate goal ID")
        return self
