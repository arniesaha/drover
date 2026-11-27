"""P2 characterization pin: verification run-key derivation (issue #1024, Lane C).

Standalone on purpose (not collected by pytest): run it against whichever
source tree is checked out.

    uv run --locked python tests/fixtures/quality-pilots/check_p2_run_key_vectors.py
    uv run --locked python tests/fixtures/quality-pilots/check_p2_run_key_vectors.py --print

Needs only the locked base project (no extras) on Python >= 3.11; see
docs/quality-pilots/README.md for the interpreter this was proven with.

Exit 0: every pinned digest matches. Exit 1: mismatch (not equivalent).
Exit 2: the pinned entry point is missing or changed shape; re-bind the pin.
"""

from __future__ import annotations

import json
import sys
from functools import partial
from pathlib import Path
from types import SimpleNamespace

VECTORS = Path(__file__).with_name("p2-run-key-vectors.json")


def derive(identity, v):
    request = identity(v["parent_run_key"], "verify", v["body"])
    run_key = identity(v["workspace_id"], v["goal_id"], v["number"], request, "verify")
    return {"request_key": request, "verify_run_key": run_key}


def stub_service(work_service, i):
    """Smallest receiver for a goal with no GitHub acceptance profile.

    Baseline 7133442d reads only ``config.workspace_id``. Merged Lane A
    (b36305f6) also resolves the goal, provider and ref, and returns the item
    body as criteria when the issue is not opted in; that is the path pinned.
    """
    goal = SimpleNamespace(goal_id=i["goal_id"], github_acceptance_profiles={})
    service = SimpleNamespace(
        config=SimpleNamespace(workspace_id=i["workspace_id"], goals=[goal]),
        providers={i["goal_id"]: object()},
        ref=lambda scope, number: SimpleNamespace(number=number),
    )
    criteria = getattr(work_service, "_verification_criteria", None)
    if criteria is not None:
        service._verification_criteria = partial(criteria, service)
    return service


def main(argv):
    try:
        from drover.server.work_queue.service import WorkService, identity

        site = WorkService._verification_request
    except (ImportError, AttributeError) as exc:
        print(f"UNBOUND: pinned entry point missing: {exc}")
        return 2
    doc = json.loads(VECTORS.read_text())
    failed = 0
    for v in doc["vectors"]:
        i = v["inputs"]
        got = derive(identity, i)
        # The real call site must agree with the written-out composition.
        try:
            via_site = site(
                stub_service(WorkService, i),
                i["goal_id"],
                i["number"],
                {"run_key": i["parent_run_key"]},
                {"body": i["body"]},
            )
        except (AttributeError, KeyError, TypeError) as exc:
            print(f"UNBOUND: call site no longer fits the pinned shape: {exc!r}")
            return 2
        if tuple(got.values()) != tuple(via_site):
            failed += 1
            print(f"MISMATCH {v['id']}: call site disagrees with composition")
            continue
        if "--print" in argv:
            print(json.dumps({"id": v["id"], **got}))
        elif got != v["expected"]:
            failed += 1
            print(f"MISMATCH {v['id']}: expected {v['expected']} got {got}")
    if "--print" not in argv:
        print(f"{len(doc['vectors']) - failed}/{len(doc['vectors'])} vectors match")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
