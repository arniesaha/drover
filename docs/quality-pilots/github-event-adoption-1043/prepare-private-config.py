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

PIN = "bde283aa6bd4f97073867930153c8d12e9b8b85e"
REVISION = "df895b4aafeb94a8d356cbcd9dfc277642e67989dc5ce9202a07f96544c95cfe"
GOAL = "github-event-adoption-1043"
ISSUE = "1043"
p = argparse.ArgumentParser()
p.add_argument("--baseline-config", type=pathlib.Path, required=True)
p.add_argument("--native-template-goal", required=True)
p.add_argument("--execution-host", required=True)
p.add_argument("--execution-cwd", type=pathlib.Path, required=True)
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
execution_cwd = a.execution_cwd.resolve()
assert execution_cwd.is_dir(), "execution cwd must be an existing isolated directory"
assert a.execution_host in native.execution_cwds, "execution host is not supported"
assert all(
    execution_cwd != pathlib.Path(template_cwd).resolve()
    for template_cwd in native.execution_cwds.values()
), "execution cwd must not reuse a native canary directory"

# Copy only validated runtime fields from the owner-selected native template and
# the existing P1 credential reference. No credential value is read or emitted.
goal = GoalScope.model_validate(
    {
        "goal_id": GOAL,
        "repo": p1.repo,
        "goal_label": "goal:" + GOAL,
        "policy_path": native.policy_path,
        "credential": p1.credential.model_dump(mode="json"),
        "execution_enabled": native.execution_enabled,
        "execution_cwds": {a.execution_host: str(execution_cwd)},
        "brief_ref": native.brief_ref,
        "execution_budget": native.execution_budget,
        "execution_estimated_cost": native.execution_estimated_cost,
        "execution_max_workers": native.execution_max_workers,
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
baseline_without_ingestion = base.model_dump(mode="json")
baseline_ingestion = baseline_without_ingestion.pop("github_provider_ingestion")
record = {
    "source": PIN,
    "profile_revision": REVISION,
    "p1_goal_digest": hashlib.sha256(
        json.dumps(p1.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest(),
    "baseline_without_ingestion_digest": hashlib.sha256(
        json.dumps(baseline_without_ingestion, sort_keys=True).encode()
    ).hexdigest(),
    "baseline_ingestion": baseline_ingestion,
    "prepared_ingestion": ingestion.model_dump(mode="json"),
    "execution_host": a.execution_host,
    "execution_cwd": str(execution_cwd),
    "work_queue": prepared.model_dump(mode="json"),
    "note": "projection only; no server config, store, session, or credential was changed",
}
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(record, indent=2))
print(json.dumps({key: record[key] for key in record if key != "work_queue"}, indent=2))
