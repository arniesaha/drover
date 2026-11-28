#!/usr/bin/env python3
"""Credential-free, source-pinned proof for the #1043 polling adoption task."""

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

PIN = "bde283aa6bd4f97073867930153c8d12e9b8b85e"
ROOT = pathlib.Path(__file__).resolve().parent
p = argparse.ArgumentParser()
p.add_argument("--source", type=pathlib.Path, required=True)
p.add_argument("--python", required=True)
p.add_argument("--output", type=pathlib.Path, required=True)
p.add_argument("--pg-dsn-env", default="DROVER_TEST_POSTGRES_DSN")
a = p.parse_args()
source = a.source.resolve()
output = a.output.resolve()
output.mkdir(parents=True, exist_ok=True)


def git(*args):
    return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()


assert git("rev-parse", "HEAD") == PIN, "wrong source"
assert not git("status", "--porcelain", "--untracked-files=no"), "dirty source"
assert not (
    output == source or source in output.parents
), "evidence must be outside candidate"
assert os.environ.get(a.pg_dsn_env), "isolated PostgreSQL DSN is required"
env = {
    key: value
    for key, value in os.environ.items()
    if key
    in {
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "SYSTEMROOT",
        "LANG",
        "LC_ALL",
        a.pg_dsn_env,
    }
}
env["PYTHONPATH"] = str(source / "src")
env["PYTHONDONTWRITEBYTECODE"] = "1"
pytest_args = [
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
    "tests/test_work_queue_github_acceptance.py::test_opted_provider_poll_rescans_pending_ci_and_restarts_without_duplicate_pair",
    "tests/test_work_queue_github_acceptance.py::test_provider_poller_isolates_target_failures_and_keeps_its_thread_alive",
    "tests/test_work_queue_github_acceptance.py::test_host_deferred_close_recovery_redelivery_rebuilds_plain_evidence_outcome",
    "--junitxml=" + str(output / "results.xml"),
]
invalid_proof = {
    "source": "0" * 40,
    "passed": True,
    "commands": [{"returncode": 0}] * 3,
    "counts": {"tests": 4, "skipped": 0, "errors": 0, "failures": 0},
    "invalid_result_rejected": {"case": "wrong_source", "rejected": True},
}
with tempfile.TemporaryDirectory(prefix="github-event-adoption-invalid-") as temp:
    invalid_path = pathlib.Path(temp) / "invalid-proof.json"
    invalid_path.write_text(json.dumps(invalid_proof))
    invalid_rejection_code = """
import json, pathlib, subprocess, sys
publisher, profile, proof = map(pathlib.Path, sys.argv[1:])
r = subprocess.run([sys.executable, '-I', str(publisher), '--profile', str(profile), '--proof', str(proof), '--dry-run'], capture_output=True, text=True)
assert r.returncode != 0, r.stdout + r.stderr
print(json.dumps({'case': 'wrong_source', 'rejected': True}))
"""
    commands = [
        ["-I", "scripts/quality/boundaries.py"],
        pytest_args,
        [
            "-c",
            invalid_rejection_code,
            str(ROOT / "publish-check.py"),
            str(ROOT / "profile-candidate.json"),
            str(invalid_path),
        ],
    ]
    results = []
    invalid_result_rejected = None
    for index, args in enumerate(commands):
        result = subprocess.run(
            [a.python, *args],
            cwd=source,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        log = result.stdout + result.stderr
        (output / f"command-{index}.log").write_text(log)
        results.append(
            {
                "argv": args,
                "returncode": result.returncode,
                "log_sha256": hashlib.sha256(log.encode()).hexdigest(),
            }
        )
        if index == 2 and result.returncode == 0:
            invalid_result_rejected = json.loads(result.stdout)
        if result.returncode:
            break
counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
if (output / "results.xml").exists():
    for suite in ET.parse(output / "results.xml").getroot().iter("testsuite"):
        for key in counts:
            counts[key] += int(suite.get(key, 0))
passed = (
    len(results) == 3
    and all(result["returncode"] == 0 for result in results)
    and counts == {"tests": 4, "failures": 0, "errors": 0, "skipped": 0}
    and invalid_result_rejected == {"case": "wrong_source", "rejected": True}
)
receipt = {
    "source": PIN,
    "lock_sha256": hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest(),
    "interpreter": subprocess.check_output(
        [a.python, "-c", "import sys;print(sys.executable);print(sys.version)"],
        env=env,
        text=True,
    ).strip(),
    "commands": results,
    "counts": counts,
    "invalid_result_rejected": invalid_result_rejected,
    "passed": passed,
    "scope": "isolated PR1042 source regression proof; not a live GitHub event-chain acceptance or configuration installation",
}
(output / "proof.json").write_text(json.dumps(receipt, indent=2))
print(json.dumps(receipt, indent=2))
sys.exit(0 if passed else 1)
