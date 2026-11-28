#!/usr/bin/env python3
"""Validate a private #1043 projection or final config without installing it."""

import argparse
import json
import pathlib
import sys
from dataclasses import asdict

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from drover.config import load_config  # noqa: E402
from drover.server.work_queue.config import WorkQueueConfig  # noqa: E402

PACKET = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PACKET))
from config_contract import (  # noqa: E402
    GOAL,
    ISSUE,
    PIN,
    REVISION,
    digest,
    expected_goal,
    expected_ingestion,
    load_profile,
    p1_goal,
)

p = argparse.ArgumentParser()
p.add_argument("--baseline-config", type=pathlib.Path, required=True)
p.add_argument("--native-template-goal", required=True)
p.add_argument("--execution-host", required=True)
p.add_argument("--execution-cwd", type=pathlib.Path, required=True)
p.add_argument("--brief-ref", required=True)
group = p.add_mutually_exclusive_group(required=True)
group.add_argument("--projection", type=pathlib.Path)
group.add_argument("--candidate-config", type=pathlib.Path)
p.add_argument("--require-binding", action="store_true")
a = p.parse_args()

baseline_config = load_config(a.baseline_config)
baseline = baseline_config.work_queue
record = None
if a.projection:
    record = json.loads(a.projection.read_text())
    assert record["source"] == PIN and record["profile_revision"] == REVISION
    assert record["execution_host"] == a.execution_host
    assert record["execution_cwd"] == str(a.execution_cwd.resolve())
    assert record["brief_ref"] == a.brief_ref
    candidate = WorkQueueConfig.model_validate(record["work_queue"])
else:
    candidate_config = load_config(a.candidate_config)
    candidate = candidate_config.work_queue
    baseline_rest = asdict(baseline_config)
    candidate_rest = asdict(candidate_config)
    baseline_rest.pop("work_queue")
    candidate_rest.pop("work_queue")
    assert candidate_rest == baseline_rest, "configuration changed outside work_queue"

assert baseline.enabled and baseline.execution.global_max_workers == 2
profile = load_profile(PACKET / "profile-candidate.json")
p1 = p1_goal(baseline)
expected = expected_goal(
    baseline,
    a.native_template_goal,
    a.execution_host,
    a.execution_cwd,
    profile,
    a.brief_ref,
)
expected_ingestion_config = expected_ingestion(baseline)
assert candidate.github_provider_ingestion.model_dump(
    mode="json"
) == expected_ingestion_config.model_dump(
    mode="json"
), "provider ingestion differs from the prepared bounded settings"


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
baseline_digest = digest(baseline_without)
if record is not None:
    assert record["baseline_without_ingestion_digest"] == baseline_digest
    assert record[
        "baseline_ingestion"
    ] == baseline.github_provider_ingestion.model_dump(mode="json")
    assert record["prepared_ingestion"] == expected_ingestion_config.model_dump(
        mode="json"
    )

targets = [goal for goal in candidate.goals if goal.goal_id == GOAL]
assert len(targets) == 1
target_dump = targets[0].model_dump(mode="json")
expected_dump = expected.model_dump(mode="json")
if a.require_binding:
    bindings = target_dump["github_existing_session_bindings"]
    assert set(bindings) == {ISSUE}, "only the exact #1043 binding is authorized"
    binding = bindings[ISSUE]
    assert binding["profile_revision"] == REVISION and binding["source_head"] == PIN
    expected_dump["github_existing_session_bindings"] = bindings
assert (
    target_dump == expected_dump
), "target goal differs from the complete prepared model"

print(
    json.dumps(
        {
            "source": PIN,
            "profile_revision": REVISION,
            "p1_goal_digest": digest(p1.model_dump(mode="json")),
            "p1_unchanged": True,
            "baseline_unchanged_outside_target_and_ingestion": True,
            "prepared_ingestion": expected_ingestion_config.model_dump(mode="json"),
            "target_goal": GOAL,
            "binding_required": a.require_binding,
            "valid": True,
        },
        indent=2,
    )
)
