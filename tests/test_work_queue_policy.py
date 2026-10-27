import json

import pytest
from pydantic import ValidationError

from drover.config import default_config, load_config
from drover.server.work_queue.config import (
    GoalPolicy,
    GoalScope,
    WorkQueueConfig,
    load_policy,
)


def scope(**changes):
    return GoalScope.model_validate(
        {
            "goal_id": "goal-1",
            "repo": "example/project",
            "goal_label": "goal:2026-w42",
            "credential": {
                "token_env": "TEST_WORK_QUEUE_TOKEN",
                "capabilities": [
                    "issues_read",
                    "pulls_read",
                    "checks_read",
                    "actions_read",
                ],
            },
            **changes,
        }
    )


def test_versioned_defaults_match_spec():
    p = load_policy()
    assert p.version == 1
    assert p.retries.attempts == 3
    assert p.capacity.goal_workers == p.capacity.workspace_workers == 5
    assert p.capacity.include_verification
    assert p.quota.reserve_fraction == 0.2 and p.quota.unknown_pauses_launch
    assert (p.progress.stale_seconds, p.progress.active_tool_seconds) == (1800, 3600)
    assert not p.progress.stale_restarts_worker
    assert (p.window.extension_limit, p.window.extension_seconds) == (1, 86400)
    assert p.window.running_may_finish and p.window.expired_launch_requires_extension
    assert (
        p.acceptance.separate_verification_run
        and p.acceptance.behavioral_evidence_required
    )
    assert p.acceptance.rerun_relevant_checks and p.acceptance.amendment_record_required
    assert (
        not p.acceptance.human_acceptance_required
        and not p.acceptance.separate_verifier_credential
    )
    assert p.authority.human_actions == [
        "production_cutover",
        "data_migration",
        "delete_data",
        "outward_post",
    ]
    assert p.authority.ambiguous_requires_decision
    assert p.priority.default == "normal"
    assert p.priority.order == ["incident", "goal_priority", "oldest_queued"]
    assert (
        p.retries.systemic_review_required
        and p.retries.new_attempt_requires_changed_approach
    )
    assert not p.retries.human_rejection_consumes_attempt
    assert p.poll.interval_seconds == p.poll.freshness_seconds == 120
    assert (p.retention.links_after_session_days, p.retention.decision_days) == (
        90,
        365,
    )


def test_goal_policy_overrides_are_loaded_and_validated(tmp_path):
    data = load_policy().model_dump()
    data["capacity"]["goal_workers"] = 2
    data["poll"]["freshness_seconds"] = 9
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    assert load_policy(str(path)).capacity.goal_workers == 2
    assert scope(policy_path=str(path)).policy_path == str(path)
    for section, field, value in [
        ("quota", "reserve_fraction", 1.0),
        ("retries", "attempts", 0),
        ("poll", "interval_seconds", -1),
    ]:
        bad = json.loads(json.dumps(data))
        bad[section][field] = value
        with pytest.raises(ValidationError):
            GoalPolicy.model_validate(bad)
    with pytest.raises(ValidationError):
        GoalPolicy.model_validate({**data, "version": 2})
    with pytest.raises(ValidationError):
        GoalPolicy.model_validate({**data, "unknown": 1})


def test_scope_and_app_config_reject_ambiguity_and_unscoped_credentials():
    assert scope(goal_label=None, milestone=7).milestone == 7
    for values in (
        {"milestone": 7},
        {"goal_label": None},
        {"repo": "../project"},
        {"goal_label": "wrong:goal"},
    ):
        # GitHub permits dot names but traversal components must not be accepted.
        with pytest.raises(ValueError):
            scope(**values)
    with pytest.raises(ValueError):
        scope(
            credential={
                "mode": "app",
                "token_env": "TOKEN",
                "capabilities": ["issues_read"],
            }
        )
    assert (
        scope(
            credential={
                "mode": "app",
                "token_env": "TOKEN",
                "installation_id": 3,
                "repository_id": 4,
                "capabilities": ["issues_read"],
            }
        ).credential.mode
        == "app"
    )
    with pytest.raises(ValueError):
        WorkQueueConfig(enabled=True)
    assert not WorkQueueConfig(shadow_mode=False).shadow_mode


def test_continuity_owner_accepts_legacy_or_its_exact_native_goal_identity():
    assert scope(continuity_owner_id="agent:coder:main").continuity_owner_id
    native = scope(
        continuity_owner_id="drover:owner/goal-1",
        execution_enabled=True,
        execution_cwds={"studio": "/workspace/project"},
        brief_ref="docs/brief.md",
        continuity_owner_harness="codex",
    )
    assert native.continuity_owner_id == "drover:owner/goal-1"
    for owner in ("drover:owner/other-goal", "drover:worker/goal-1", "owner"):
        with pytest.raises(ValidationError):
            scope(continuity_owner_id=owner)


def test_runtime_config_defaults_and_toml_opt_in(tmp_path):
    assert not default_config().work_queue.enabled
    path = tmp_path / "config.toml"
    path.write_text("""[work_queue]
enabled = true
workspace_id = "workspace-test"
[[work_queue.goals]]
goal_id = "goal-1"
repo = "example/project"
milestone = 7
[work_queue.goals.credential]
token_env = "TEST_WORK_QUEUE_TOKEN"
capabilities = ["issues_read"]
""")
    cfg = load_config(path).work_queue
    assert cfg.enabled and cfg.shadow_mode and cfg.goals[0].milestone == 7
