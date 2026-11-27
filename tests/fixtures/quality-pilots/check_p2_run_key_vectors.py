"""P2 characterization pin: verification run-key derivation (issue #1024, Lane C).

Standalone on purpose (not collected by pytest): run it against whichever
source tree is checked out.

    uv run --locked python tests/fixtures/quality-pilots/check_p2_run_key_vectors.py
    uv run --locked python tests/fixtures/quality-pilots/check_p2_run_key_vectors.py --print

Exit 0: every pinned digest matches. Exit 1: mismatch (not equivalent).
Exit 2: the pinned entry point is missing, so the pin must be re-bound.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

VECTORS = Path(__file__).with_name("p2-run-key-vectors.json")


def derive(identity, v):
    request = identity(v["parent_run_key"], "verify", v["body"])
    run_key = identity(v["workspace_id"], v["goal_id"], v["number"], request, "verify")
    return {"request_key": request, "verify_run_key": run_key}


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
        service = SimpleNamespace(
            config=SimpleNamespace(workspace_id=i["workspace_id"])
        )
        via_site = site(
            service,
            i["goal_id"],
            i["number"],
            {"run_key": i["parent_run_key"]},
            {"body": i["body"]},
        )
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
