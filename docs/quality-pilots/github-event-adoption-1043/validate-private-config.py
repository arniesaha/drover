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

PIN = "f68653d9be5adbe8eed1ba53a832d5ebb55d7dbc"
REVISION = "e126e03963bcb5cf65c1db3d1f017ab9c70c293ce54309874ea030c26d84205b"
GOAL = "github-event-adoption-1043"
ISSUE = "1043"
p = argparse.ArgumentParser()
p.add_argument("--baseline-config", type=pathlib.Path, required=True)
group = p.add_mutually_exclusive_group(required=True)
group.add_argument("--projection", type=pathlib.Path)
group.add_argument("--candidate-config", type=pathlib.Path)
p.add_argument("--require-binding", action="store_true")
a = p.parse_args()

baseline = load_config(a.baseline_config).work_queue
if a.projection:
    record = json.loads(a.projection.read_text())
    assert record["source"] == PIN and record["profile_revision"] == REVISION
    candidate = WorkQueueConfig.model_validate(record["work_queue"])
else:
    candidate = load_config(a.candidate_config).work_queue

before = next(goal for goal in baseline.goals if goal.goal_id == "quality-cohort-1024")
after = next(goal for goal in candidate.goals if goal.goal_id == "quality-cohort-1024")
before_digest = hashlib.sha256(
    json.dumps(before.model_dump(mode="json"), sort_keys=True).encode()
).hexdigest()
after_digest = hashlib.sha256(
    json.dumps(after.model_dump(mode="json"), sort_keys=True).encode()
).hexdigest()
assert before_digest == after_digest, "P1 goal changed"
targets = [goal for goal in candidate.goals if goal.goal_id == GOAL]
assert len(targets) == 1
target = targets[0]
assert candidate.enabled and candidate.execution.global_max_workers == 2
assert target.repo == "arniesaha/drover"
assert target.goal_label == "goal:" + GOAL
assert target.continuity_owner_id == "drover:owner/" + GOAL
assert target.integration_serialization_key == GOAL
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
            "target_goal": GOAL,
            "binding_required": a.require_binding,
            "valid": True,
        },
        indent=2,
    )
)
