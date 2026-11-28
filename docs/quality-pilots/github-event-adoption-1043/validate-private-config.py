#!/usr/bin/env python3
"""Validate a private #1043 projection or final config without installing it."""

import argparse
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from drover.config import load_config  # noqa: E402
from drover.server.work_queue.config import WorkQueueConfig  # noqa: E402

PIN = "bde283aa6bd4f97073867930153c8d12e9b8b85e"
REVISION = "df895b4aafeb94a8d356cbcd9dfc277642e67989dc5ce9202a07f96544c95cfe"
GOAL = "github-event-adoption-1043"
ISSUE = "1043"
p = argparse.ArgumentParser()
p.add_argument("--baseline-config", type=pathlib.Path, required=True)
p.add_argument("--native-template-goal", required=True)
p.add_argument("--execution-host", required=True)
p.add_argument("--execution-cwd", type=pathlib.Path, required=True)
group = p.add_mutually_exclusive_group(required=True)
group.add_argument("--projection", type=pathlib.Path)
group.add_argument("--candidate-config", type=pathlib.Path)
p.add_argument("--require-binding", action="store_true")
a = p.parse_args()

baseline = load_config(a.baseline_config).work_queue
if a.projection:
    record = json.loads(a.projection.read_text())
    assert record["source"] == PIN and record["profile_revision"] == REVISION
    assert record["execution_host"] == a.execution_host
    assert record["execution_cwd"] == str(a.execution_cwd.resolve())
    candidate = WorkQueueConfig.model_validate(record["work_queue"])
else:
    candidate = load_config(a.candidate_config).work_queue

expected_ingestion = {
    "enabled": True,
    "interval_seconds": 60,
    "batch_size": 2,
    "retry_seconds": 60,
    "max_backoff_seconds": 300,
    "settled_seconds": 300,
}
assert candidate.github_provider_ingestion.model_dump(mode="json") == expected_ingestion


def without_authorized_delta(config):
    material = config.model_dump(mode="json")
    material["goals"] = [goal for goal in material["goals"] if goal["goal_id"] != GOAL]
    material.pop("github_provider_ingestion")
    return material


baseline_without = without_authorized_delta(baseline)
candidate_without = without_authorized_delta(candidate)
assert (
    candidate_without == baseline_without
), "work queue changed outside target/ingestion"
baseline_digest = hashlib.sha256(
    json.dumps(baseline_without, sort_keys=True).encode()
).hexdigest()
if a.projection:
    assert record["baseline_without_ingestion_digest"] == baseline_digest
    assert record[
        "baseline_ingestion"
    ] == baseline.github_provider_ingestion.model_dump(mode="json")
    assert record["prepared_ingestion"] == expected_ingestion

before = next(goal for goal in baseline.goals if goal.goal_id == "quality-cohort-1024")
before_digest = hashlib.sha256(
    json.dumps(before.model_dump(mode="json"), sort_keys=True).encode()
).hexdigest()
targets = [goal for goal in candidate.goals if goal.goal_id == GOAL]
assert len(targets) == 1
target = targets[0]
template = next(
    goal for goal in baseline.goals if goal.goal_id == a.native_template_goal
)
execution_cwd = a.execution_cwd.resolve()
assert execution_cwd.is_dir(), "execution cwd must be an existing isolated directory"
assert a.execution_host in template.execution_cwds, "execution host is not supported"
assert all(
    execution_cwd != pathlib.Path(template_cwd).resolve()
    for template_cwd in template.execution_cwds.values()
), "execution cwd must not reuse a native canary directory"
assert candidate.enabled and candidate.execution.global_max_workers == 2
assert target.repo == "arniesaha/drover"
assert target.goal_label == "goal:" + GOAL
assert target.continuity_owner_id == "drover:owner/" + GOAL
assert target.integration_serialization_key == GOAL
assert target.execution_cwds == {a.execution_host: str(execution_cwd)}
for field in (
    "policy_path",
    "execution_enabled",
    "brief_ref",
    "execution_budget",
    "execution_estimated_cost",
    "execution_max_workers",
    "continuity_owner_harness",
    "continuity_owner_model",
):
    assert getattr(target, field) == getattr(template, field), (
        "target differs from approved native template: " + field
    )
assert target.credential.token_env == "DROVER_WORK_QUEUE_GITHUB_TOKEN"
assert {ISSUE} == set(target.github_acceptance_profiles)
profile = target.github_acceptance_profiles[ISSUE]
assert profile.pull_number == 1042 and profile.revision == REVISION
assert profile.review_mode == "independent_check" and profile.owner_id is None
assert target.github_event_delivery_profiles == {ISSUE: REVISION}
assert target.github_provider_ingestion_profiles == {ISSUE: REVISION}
assert candidate.github_provider_ingestion.enabled
assert candidate.github_provider_ingestion.interval_seconds >= 60
assert candidate.github_provider_ingestion.retry_seconds >= 60
if a.require_binding:
    binding = target.github_existing_session_bindings[ISSUE]
    assert binding.profile_revision == REVISION and binding.source_head == PIN
else:
    assert (
        not target.github_existing_session_bindings
    ), "bindings await fresh retained turns"
print(
    json.dumps(
        {
            "source": PIN,
            "profile_revision": REVISION,
            "p1_goal_digest": before_digest,
            "p1_unchanged": True,
            "baseline_unchanged_outside_target_and_ingestion": True,
            "prepared_ingestion": expected_ingestion,
            "target_goal": GOAL,
            "binding_required": a.require_binding,
            "valid": True,
        },
        indent=2,
    )
)
