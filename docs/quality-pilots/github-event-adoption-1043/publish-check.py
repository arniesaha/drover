#!/usr/bin/env python3
"""Protected-main publisher for the #1043 source-pinned proof only."""

import argparse
import hashlib
import json
import os
import pathlib
import urllib.request

PIN = "f68653d9be5adbe8eed1ba53a832d5ebb55d7dbc"
REPO = "arniesaha/drover"
p = argparse.ArgumentParser()
p.add_argument("--profile", type=pathlib.Path, required=True)
p.add_argument("--proof", type=pathlib.Path, required=True)
p.add_argument("--dry-run", action="store_true")
a = p.parse_args()
profile = json.loads(a.profile.read_text())
proof = json.loads(a.proof.read_text())
assert profile["pull_number"] == 1042 and profile["app_id"] == 15368
assert profile["review_mode"] == "independent_check"
assert "owner_id" not in profile
assert profile["behavioral_check"] == "github-event-adoption-1043-outcome"
assert proof["source"] == PIN and proof["passed"] is True
assert len(proof["commands"]) == 3 and all(
    result["returncode"] == 0 for result in proof["commands"]
)
assert proof["counts"] == {"tests": 4, "skipped": 0, "errors": 0, "failures": 0}
assert proof["invalid_result_rejected"] == {"case": "wrong_source", "rejected": True}
revision_profile = dict(profile, owner_id=None)
revision = hashlib.sha256(
    json.dumps(revision_profile, sort_keys=True).encode()
).hexdigest()
summary = (
    "Independent PR1042 polling proof: one target failure did not starve another; "
    "restart/replay remained idempotent; and an owner-held invalid proof was rejected. "
    "Installation and live event-chain acceptance remain separate."
)
text = json.dumps(proof, sort_keys=True, indent=2)
assert len(text.encode()) < 40000
payload = {
    "name": profile["behavioral_check"],
    "head_sha": PIN,
    "status": "completed",
    "conclusion": "success",
    "external_id": revision,
    "output": {
        "title": "Pinned GitHub event-adoption outcome",
        "summary": summary,
        "text": text,
    },
}
if a.dry_run:
    print(json.dumps({"profile_revision": revision, "payload": payload}, indent=2))
    raise SystemExit
token = os.environ["GITHUB_TOKEN"]
base = "https://api.github.com/repos/" + REPO
headers = {
    "Authorization": "Bearer " + token,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def request(method, path, body=None):
    req = urllib.request.Request(
        base + path,
        headers=headers,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


pull = request("GET", "/pulls/1042")
assert pull["head"]["sha"] == PIN
existing = request("GET", "/commits/" + PIN + "/check-runs?per_page=100")["check_runs"]
matching = [
    check
    for check in existing
    if check["name"] == payload["name"] and check.get("external_id") == revision
]
if matching:
    assert (
        len(matching) == 1
    ), "ambiguous prior publication; inspect, never blindly retry"
    old = request("GET", "/check-runs/" + str(matching[0]["id"]))
    assert all(
        old.get(key) == payload[key]
        for key in ("head_sha", "conclusion", "external_id")
    )
    assert all(
        old["output"][key] == payload["output"][key] for key in ("summary", "text")
    )
    print(json.dumps({"reused_check_id": old["id"]}))
else:
    result = request("POST", "/check-runs", payload)
    print(json.dumps({"created_check_id": result["id"]}))
