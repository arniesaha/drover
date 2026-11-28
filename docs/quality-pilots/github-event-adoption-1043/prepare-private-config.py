#!/usr/bin/env python3
"""Build a private, non-installing #1043 work-queue projection from live config."""

import argparse
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from drover.config import load_config  # noqa: E402
from drover.server.work_queue.config import GoalScope, WorkQueueConfig  # noqa: E402

PIN = "f68653d9be5adbe8eed1ba53a832d5ebb55d7dbc"
REVISION = "e126e03963bcb5cf65c1db3d1f017ab9c70c293ce54309874ea030c26d84205b"
GOAL = "github-event-adoption-1043"
ISSUE = "1043"
p = argparse.ArgumentParser()
p.add_argument("--baseline-config", type=pathlib.Path, required=True)
p.add_argument("--native-template-goal", required=True)
p.add_argument("--profile", type=pathlib.Path, required=True)
p.add_argument("--output", type=pathlib.Path, required=True)
a = p.parse_args()

base = load_config(a.baseline_config).work_queue
assert (
    base.enabled and base.execution.global_max_workers == 2
), "requires configured cap two"
profile = json.loads(a.profile.read_text())
assert (
    hashlib.sha256(
        json.dumps(dict(profile, owner_id=None), sort_keys=True).encode()
    ).hexdigest()
    == REVISION
)
p1 = next(goal for goal in base.goals if goal.goal_id == "quality-cohort-1024")
assert set(p1.github_acceptance_profiles) == {"1031"}
assert (
    not p1.github_event_delivery_profiles and not p1.github_provider_ingestion_profiles
)
native = next(goal for goal in base.goals if goal.goal_id == a.native_template_goal)
assert native.continuity_owner_id == "drover:owner/" + native.goal_id
assert native.execution_enabled and native.execution_cwds and native.brief_ref
assert native.continuity_owner_harness and native.continuity_owner_model

# Copy only validated runtime fields from the owner-selected native template and
# the existing P1 credential reference. No credential value is read or emitted.
goal = GoalScope.model_validate(
    {
        "goal_id": GOAL,
        "repo": p1.repo,
        "goal_label": "goal:" + GOAL,
        "credential": p1.credential.model_dump(mode="json"),
        "execution_enabled": native.execution_enabled,
        "execution_cwds": native.execution_cwds,
        "brief_ref": native.brief_ref,
        "continuity_owner_id": "drover:owner/" + GOAL,
        "continuity_owner_harness": native.continuity_owner_harness,
        "continuity_owner_model": native.continuity_owner_model,
        "candidate_source": "github",
        "integration_serialization_key": GOAL,
        "github_acceptance_profiles": {ISSUE: profile},
        "github_event_delivery_profiles": {ISSUE: REVISION},
        "github_provider_ingestion_profiles": {ISSUE: REVISION},
        "github_existing_session_bindings": {},
    }
)
ingestion = base.github_provider_ingestion.model_copy(
    update={
        "enabled": True,
        "interval_seconds": 60,
        "batch_size": 2,
        "retry_seconds": 60,
        "max_backoff_seconds": 300,
        "settled_seconds": 300,
    }
)
prepared = base.model_copy(
    update={"goals": [*base.goals, goal], "github_provider_ingestion": ingestion}
)
# Revalidate the entire private projection before it leaves this process.
prepared = WorkQueueConfig.model_validate(prepared.model_dump(mode="json"))
record = {
    "source": PIN,
    "profile_revision": REVISION,
    "p1_goal_digest": hashlib.sha256(
        json.dumps(p1.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest(),
    "work_queue": prepared.model_dump(mode="json"),
    "note": "projection only; no server config, store, session, or credential was changed",
}
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(record, indent=2))
print(json.dumps({key: record[key] for key in record if key != "work_queue"}, indent=2))
