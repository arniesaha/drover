#!/usr/bin/env python3
"""Build a private, non-installing #1043 work-queue projection from live config."""

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from drover.config import load_config  # noqa: E402
from drover.server.work_queue.config import WorkQueueConfig  # noqa: E402

PACKET = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PACKET))
from config_contract import (  # noqa: E402
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
p.add_argument("--profile", type=pathlib.Path, required=True)
p.add_argument("--output", type=pathlib.Path, required=True)
a = p.parse_args()

base = load_config(a.baseline_config).work_queue
assert (
    base.enabled and base.execution.global_max_workers == 2
), "requires configured cap two"
# The expected goal copies only reviewed template fields and the P1 credential reference.
# It never reads or emits a credential value.
profile = load_profile(a.profile)
p1 = p1_goal(base)
goal = expected_goal(
    base,
    a.native_template_goal,
    a.execution_host,
    a.execution_cwd,
    profile,
    a.brief_ref,
)
ingestion = expected_ingestion(base)
execution_cwd = a.execution_cwd.resolve()
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
    "p1_goal_digest": digest(p1.model_dump(mode="json")),
    "baseline_without_ingestion_digest": digest(baseline_without_ingestion),
    "baseline_ingestion": baseline_ingestion,
    "prepared_ingestion": ingestion.model_dump(mode="json"),
    "execution_host": a.execution_host,
    "execution_cwd": str(execution_cwd),
    "brief_ref": goal.brief_ref,
    "work_queue": prepared.model_dump(mode="json"),
    "note": "projection only; no server config, store, session, or credential was changed",
}
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(record, indent=2))
print(json.dumps({key: record[key] for key in record if key != "work_queue"}, indent=2))
