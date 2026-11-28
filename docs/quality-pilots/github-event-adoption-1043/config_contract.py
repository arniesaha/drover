"""Shared, non-installing #1043 configuration contract."""

import hashlib
import json
from pathlib import Path

from drover.server.work_queue.config import GitHubAcceptanceProfile, GoalScope

PIN = "bde283aa6bd4f97073867930153c8d12e9b8b85e"
REVISION = "df895b4aafeb94a8d356cbcd9dfc277642e67989dc5ce9202a07f96544c95cfe"
GOAL = "github-event-adoption-1043"
ISSUE = "1043"
EXPECTED_INGESTION = {
    "enabled": True,
    "interval_seconds": 60,
    "batch_size": 2,
    "retry_seconds": 60,
    "max_backoff_seconds": 300,
    "settled_seconds": 300,
}


def load_profile(path: Path) -> GitHubAcceptanceProfile:
    profile = GitHubAcceptanceProfile.model_validate(json.loads(path.read_text()))
    assert profile.revision == REVISION
    return profile


def p1_goal(base):
    p1 = next(goal for goal in base.goals if goal.goal_id == "quality-cohort-1024")
    assert set(p1.github_acceptance_profiles) == {"1031"}
    assert (
        not p1.github_event_delivery_profiles
        and not p1.github_provider_ingestion_profiles
    )
    return p1


def template_and_cwd(base, template_goal_id: str, host: str, cwd: Path):
    native = next(goal for goal in base.goals if goal.goal_id == template_goal_id)
    assert native.continuity_owner_id == "drover:owner/" + native.goal_id
    assert native.execution_enabled and native.execution_cwds and native.brief_ref
    assert native.continuity_owner_harness and native.continuity_owner_model
    resolved_cwd = cwd.resolve()
    assert resolved_cwd.is_dir(), "execution cwd must be an existing isolated directory"
    assert host in native.execution_cwds, "execution host is not supported"
    template_cwds = [Path(value).resolve() for value in native.execution_cwds.values()]
    assert all(
        not (
            resolved_cwd.is_relative_to(template_cwd)
            or template_cwd.is_relative_to(resolved_cwd)
        )
        for template_cwd in template_cwds
    ), "execution cwd must not overlap a native canary directory"
    return native, resolved_cwd


def expected_goal(base, template_goal_id: str, host: str, cwd: Path, profile):
    p1 = p1_goal(base)
    native, resolved_cwd = template_and_cwd(base, template_goal_id, host, cwd)
    return GoalScope.model_validate(
        {
            "goal_id": GOAL,
            "repo": p1.repo,
            "goal_label": "goal:" + GOAL,
            "policy_path": native.policy_path,
            "credential": p1.credential.model_dump(mode="json"),
            "execution_enabled": native.execution_enabled,
            "execution_cwds": {host: str(resolved_cwd)},
            "brief_ref": native.brief_ref,
            "execution_budget": native.execution_budget,
            "execution_estimated_cost": native.execution_estimated_cost,
            "execution_max_workers": native.execution_max_workers,
            "continuity_owner_id": "drover:owner/" + GOAL,
            "continuity_owner_harness": native.continuity_owner_harness,
            "continuity_owner_model": native.continuity_owner_model,
            "candidate_source": "github",
            "integration_serialization_key": GOAL,
            "github_acceptance_profiles": {ISSUE: profile.model_dump(mode="json")},
            "github_event_delivery_profiles": {ISSUE: REVISION},
            "github_provider_ingestion_profiles": {ISSUE: REVISION},
            "github_existing_session_bindings": {},
        }
    )


def expected_ingestion(base):
    return base.github_provider_ingestion.model_copy(update=EXPECTED_INGESTION)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
