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
    # Local manifests cannot select authority. A deployment scope is available
    # only when this reviewed policy opt-in and a goal configuration selector
    # name the exact candidate number.
    local_candidate_deployment_scope_enabled: bool = False
    # Committed local provenance is disabled by default. A reviewed policy and
    # exact candidate selector are both required before a local worker may make
    # an isolated session-branch commit for a separate verifier.
    local_candidate_committed_provenance_enabled: bool = False
    # A reviewed M2-only receipt handoff is disabled by default.  It permits
    # one exact local candidate to stop after its durable effect receipt so a
    # real hub restart can exercise successor-epoch acknowledgement recovery.
    local_candidate_m2_ack_pending_enabled: bool = False


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


class GitHubAcceptanceProfile(Model):
    """Reviewed server-side pilot contract; never sourced from candidate text."""

    pull_number: int = Field(gt=0)
    criteria: str = Field(min_length=1, max_length=16000)
    instructions: str = Field(min_length=1, max_length=16000)
    behavioral_check: str = Field(min_length=1, max_length=100)
    app_id: int = Field(gt=0)
    # The historical human mode remains the default.  An exact profile may
    # instead select the independently configured behavioral check as its
    # verdict source; callers can never select this mode.
    review_mode: Literal["human_approval", "independent_check"] = "human_approval"
    owner_id: int | None = Field(default=None, gt=0)
    amendment_record: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def valid(self):
        if self.review_mode == "human_approval" and self.owner_id is None:
            raise ValueError("human approval mode requires an owner id")
        if self.review_mode == "independent_check" and self.owner_id is not None:
            raise ValueError("independent check mode cannot select a human reviewer")
        return self

    @property
    def revision(self):
        import hashlib

        return hashlib.sha256(
            json.dumps(self.model_dump(), sort_keys=True).encode()
        ).hexdigest()


class GitHubExistingSessionBinding(Model):
    """One owner-reviewed reuse of already completed coordinator sessions.

    This is deliberately outside ``GitHubAcceptanceProfile``: it binds a
    reviewed profile revision to retained session/turn records without
    changing the profile's evidence revision after its check has published.
    """

    profile_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    work_session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,190}$")
    work_turn_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,190}$")
    verifier_session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,190}$")
    verifier_turn_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,190}$")

    @model_validator(mode="after")
    def valid(self):
        if self.work_session_id == self.verifier_session_id:
            raise ValueError("existing work and verifier sessions must be separate")
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
    # An explicitly configured conflicting-write class.  This is a logical
    # delivery role only: it reuses this goal's native owner and normal
    # implementation authority.
    integration_serialization_key: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,190}$"
    )
    # When present, only an operator-authorized candidate carrying this label
    # enters the configured conflicting-write class.  Omitting the selector
    # preserves the historical all-work serialization behavior.
    integration_serialization_label: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,190}$"
    )
    # Explicit opt-in: the one OpenClaw session allowed to receive durable
    # work-completion wakes for this goal.  No default owner exists.
    continuity_owner_id: str | None = None
    # Native owners use the normal structured harness launch contract.  The
    # harness is deliberately explicit: a native owner must not inherit a
    # provider/model default from an unrelated worker.
    continuity_owner_harness: str | None = None
    continuity_owner_model: str | None = None
    # GitHub is the normal ingress.  ``local_file`` is an explicit isolated
    # canary source and never inherits a remote provider credential.
    candidate_source: Literal["github", "local_file"] = "github"
    # Exact item opt-in, installed by an independent owner after review.
    github_acceptance_profiles: dict[str, GitHubAcceptanceProfile] = Field(
        default_factory=dict
    )
    # An exact owner-reviewed reuse mapping for sessions that predate the
    # queue link. It is opt-in and keyed to the same exact issue as its
    # acceptance profile; neither a caller nor a session transcript selects it.
    github_existing_session_bindings: dict[str, GitHubExistingSessionBinding] = Field(
        default_factory=dict
    )
    # A separate, exact-issue opt-in for the pushed GitHub CI continuation.
    # Keeping this outside the profile preserves already-installed profile
    # revisions and makes the execution path an explicit rollout decision.
    github_event_delivery_profiles: dict[str, str] = Field(default_factory=dict)
    # A separate exact-issue opt-in for authenticated GitHub API polling.  It
    # observes the current source and hands it to the same revalidating CI
    # continuation as a signed delivery; it is never a webhook substitute.
    github_provider_ingestion_profiles: dict[str, str] = Field(default_factory=dict)
    local_candidates_path: str | None = None
    # Per-item worktrees are local-file-only and must be declared in the
    # server configuration; manifest content cannot choose an arbitrary path.
    local_candidate_worktrees: dict[str, str] = Field(default_factory=dict)
    # A deployment authority is selected only by the reviewed goal config for
    # an exact local candidate. Manifest labels and bodies cannot choose it.
    local_candidate_authority_scopes: dict[str, Literal["deployment"]] = Field(
        default_factory=dict
    )
    # An M3 committed-provenance exception is selected only by reviewed goal
    # configuration for an exact local candidate. Manifest bodies cannot grant
    # it, and verifier runs never inherit it.
    local_candidate_committed_provenance: dict[str, Literal["m3"]] = Field(
        default_factory=dict
    )
    # M2 restart evidence is selected only by reviewed config for one exact
    # local candidate. Task bodies and manifests cannot request this handoff.
    local_candidate_m2_ack_pending: dict[str, Literal["m2"]] = Field(
        default_factory=dict
    )
    credential: GitHubCredential

    @model_validator(mode="after")
    def valid(self):
        native_owner = "drover:owner/" + self.goal_id
        if self.github_acceptance_profiles:
            if self.candidate_source != "github" or any(
                not key.isascii()
                or not key.isdigit()
                or str(int(key)) != key
                or int(key) <= 0
                for key in self.github_acceptance_profiles
            ):
                raise ValueError("GitHub acceptance profiles require exact issue keys")
            if not {"issues_read", "pulls_read", "checks_read"}.issubset(
                self.credential.capabilities
            ):
                raise ValueError("GitHub acceptance profiles require provider reads")
        if self.github_existing_session_bindings:
            if self.candidate_source != "github" or any(
                not key.isascii()
                or not key.isdigit()
                or str(int(key)) != key
                or int(key) <= 0
                for key in self.github_existing_session_bindings
            ):
                raise ValueError("existing session bindings require exact issue keys")
            for key, binding in self.github_existing_session_bindings.items():
                profile = self.github_acceptance_profiles.get(key)
                if profile is None:
                    raise ValueError(
                        "existing session binding requires an acceptance profile"
                    )
                if binding.profile_revision != profile.revision:
                    raise ValueError(
                        "existing session binding profile revision mismatch"
                    )
        if self.github_event_delivery_profiles:
            if (
                self.candidate_source != "github"
                or self.continuity_owner_id != native_owner
            ):
                raise ValueError(
                    "GitHub event delivery requires GitHub source and its native owner"
                )
            for key, revision in self.github_event_delivery_profiles.items():
                profile = self.github_acceptance_profiles.get(key)
                if (
                    not key.isascii()
                    or not key.isdigit()
                    or str(int(key)) != key
                    or int(key) <= 0
                    or profile is None
                    or revision != profile.revision
                ):
                    raise ValueError(
                        "GitHub event delivery requires an exact profile revision"
                    )
        if self.github_provider_ingestion_profiles:
            if (
                self.candidate_source != "github"
                or self.continuity_owner_id != native_owner
            ):
                raise ValueError(
                    "GitHub provider ingestion requires GitHub source and its native owner"
                )
            for key, revision in self.github_provider_ingestion_profiles.items():
                profile = self.github_acceptance_profiles.get(key)
                if (
                    not key.isascii()
                    or not key.isdigit()
                    or str(int(key)) != key
                    or int(key) <= 0
                    or profile is None
                    or revision != profile.revision
                    or self.github_event_delivery_profiles.get(key) != revision
                ):
                    raise ValueError(
                        "GitHub provider ingestion requires an installed exact delivery profile"
                    )
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
        if self.integration_serialization_key is not None and owner != native_owner:
            raise ValueError(
                "integration serialization requires this goal's native owner"
            )
        if (
            self.integration_serialization_label is not None
            and self.integration_serialization_key is None
        ):
            raise ValueError(
                "integration serialization label requires a serialization key"
            )
        if self.candidate_source == "local_file":
            if (
                not self.local_candidates_path
                or not Path(self.local_candidates_path).is_absolute()
            ):
                raise ValueError("local candidate source requires an absolute manifest")
            for number, path in self.local_candidate_worktrees.items():
                if (
                    not number.isdigit()
                    or int(number) <= 0
                    or not Path(path).is_absolute()
                ):
                    raise ValueError(
                        "local candidate worktrees require positive item keys and absolute paths"
                    )
            if self.local_candidate_authority_scopes:
                if owner != native_owner:
                    raise ValueError(
                        "local deployment scope requires this goal's native owner"
                    )
                if not policy.authority.local_candidate_deployment_scope_enabled:
                    raise ValueError("local deployment scope requires policy opt-in")
                if any(
                    not number.isdigit() or int(number) <= 0
                    for number in self.local_candidate_authority_scopes
                ):
                    raise ValueError(
                        "local deployment scope requires positive item keys"
                    )
            if self.local_candidate_committed_provenance:
                if owner != native_owner:
                    raise ValueError(
                        "local committed provenance requires this goal's native owner"
                    )
                if not policy.authority.local_candidate_committed_provenance_enabled:
                    raise ValueError(
                        "local committed provenance requires policy opt-in"
                    )
                if any(
                    not number.isdigit() or int(number) <= 0
                    for number in self.local_candidate_committed_provenance
                ):
                    raise ValueError(
                        "local committed provenance requires positive item keys"
                    )
                if set(self.local_candidate_committed_provenance) & set(
                    self.local_candidate_authority_scopes
                ):
                    raise ValueError(
                        "local committed provenance cannot select deployment authority"
                    )
            if self.local_candidate_m2_ack_pending:
                if owner != native_owner:
                    raise ValueError(
                        "local M2 handoff requires this goal's native owner"
                    )
                if not policy.authority.local_candidate_m2_ack_pending_enabled:
                    raise ValueError("local M2 handoff requires policy opt-in")
                if any(
                    not number.isdigit() or int(number) <= 0
                    for number in self.local_candidate_m2_ack_pending
                ):
                    raise ValueError("local M2 handoff requires positive item keys")
                if set(self.local_candidate_m2_ack_pending) & (
                    set(self.local_candidate_authority_scopes)
                    | set(self.local_candidate_committed_provenance)
                ):
                    raise ValueError(
                        "local M2 handoff cannot select deployment or committed provenance"
                    )
        elif (
            self.local_candidates_path is not None
            or self.local_candidate_worktrees
            or self.local_candidate_authority_scopes
            or self.local_candidate_committed_provenance
            or self.local_candidate_m2_ack_pending
        ):
            raise ValueError(
                "GitHub candidate source cannot configure local candidate paths"
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


class GitHubProviderIngestionConfig(Model):
    """Explicit, bounded authenticated API polling for opted-in GitHub items."""

    enabled: bool = False
    # This is intentionally a low-rate repair/adoption reader, never a
    # replacement for pushed ingress or a task-stage driver.
    interval_seconds: int = Field(default=60, ge=60, le=3600)
    batch_size: int = Field(default=8, ge=1, le=100)
    retry_seconds: int = Field(default=60, ge=60, le=3600)
    max_backoff_seconds: int = Field(default=300, ge=1, le=3600)
    settled_seconds: int = Field(default=300, ge=60, le=3600)

    @model_validator(mode="after")
    def valid(self):
        if self.max_backoff_seconds < self.retry_seconds:
            raise ValueError("github provider ingestion max_backoff must cover retry")
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


class AuditedDelegationConfig(Model):
    """Explicit server-side opt-in for one-use paired-human delegations."""

    enabled: bool = False
    max_lifetime_seconds: int = Field(default=3600, ge=60, le=86_400)


class QueueExecutionConfig(Model):
    global_max_workers: int = Field(default=1, ge=1)
    # Awaiting-input workers remain resumable, but cannot reserve capacity forever.
    idle_awaiting_input_minutes: int = Field(default=30, ge=1, le=10_080)
    orphan_intent_minutes: int = Field(default=10, ge=1, le=1_440)
    kill_switch: bool = False


class WorkQueueConfig(Model):
    enabled: bool = False
    shadow_mode: bool = True
    workspace_id: str = ""
    goals: list[GoalScope] = Field(default_factory=list)
    principal_roles: dict[str, str] = Field(default_factory=dict)
    work_item_events: WorkItemEventsConfig = Field(default_factory=WorkItemEventsConfig)
    github_webhook: GitHubWebhookConfig = Field(default_factory=GitHubWebhookConfig)
    github_provider_ingestion: GitHubProviderIngestionConfig = Field(
        default_factory=GitHubProviderIngestionConfig
    )
    rules: WorkItemRulesConfig = Field(default_factory=WorkItemRulesConfig)
    github_approvals: GitHubApprovalConfig = Field(default_factory=GitHubApprovalConfig)
    audited_delegation: AuditedDelegationConfig = Field(
        default_factory=AuditedDelegationConfig
    )
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
        keys = [
            (goal.repo, goal.integration_serialization_key)
            for goal in self.goals
            if goal.integration_serialization_key is not None
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate integration serialization repository/key")
        return self
