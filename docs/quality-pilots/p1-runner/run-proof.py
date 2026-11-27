#!/usr/bin/env python3
"""Owner-held pinned proof driver; no credential access, writes only --output."""

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import xml.etree.ElementTree as ET

PIN = "5202c2359483a58b2916c58acac5f11ee2df20b8"
p = argparse.ArgumentParser()
p.add_argument("--source", type=pathlib.Path, required=True)
p.add_argument("--python", required=True)
p.add_argument("--output", type=pathlib.Path, required=True)
a = p.parse_args()
root = a.source.resolve()
out = a.output.resolve()
out.mkdir(parents=True, exist_ok=True)


def git(*args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


assert git("rev-parse", "HEAD") == PIN, "wrong source"
assert not git("status", "--porcelain", "--untracked-files=no"), "dirty source"
assert not (out == root or root in out.parents), "evidence must be outside candidate"
env = {
    k: v
    for k, v in os.environ.items()
    if k in {"PATH", "HOME", "TMPDIR", "TEMP", "SYSTEMROOT", "LANG", "LC_ALL"}
}
env["PYTHONPATH"] = str(root / "src")
env["PYTHONDONTWRITEBYTECODE"] = "1"
commands = [
    ["-I", "scripts/quality/boundaries.py"],
    [
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "tests/test_quota_control_boundary.py",
        "tests/test_provider_quota_routing.py",
        "tests/test_quality_boundaries.py::test_real_source_passes_strict_without_known_edges",
        "tests/test_quality_boundaries.py::test_seeded_real_tree_import_of_lake_fails_strict",
        "tests/test_quality_boundaries.py::test_seeded_forbidden_dependency_fails_with_its_path",
        "--junitxml=" + str(out / "results.xml"),
    ],
]
# A second negative is constructed here, outside candidate expectations.
seed_code = r"""
import contextlib,importlib.util,io,json,pathlib,shutil,sys,tempfile
root=pathlib.Path.cwd()
spec=importlib.util.spec_from_file_location('owner_boundary',root/'scripts/quality/boundaries.py')
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='cohort1024-owner-seed-') as temp:
    src=pathlib.Path(temp)/'src';shutil.copytree(root/'src/drover',src/'drover',ignore=shutil.ignore_patterns('__pycache__'))
    target=src/'drover/server/work_queue/provider.py'
    target.write_text(target.read_text()+'\nimport drover.server.lake.owner_heldout\n')
    output=io.StringIO()
    with contextlib.redirect_stdout(output):code=m.main([],src=src)
    report=json.loads(output.getvalue())
    hits=report['boundaries']['work-coordinator-control']
    assert code==1 and report['classification']=='violation' and any(hit['forbidden']=='drover.server.lake' and hit['path'][0].startswith('drover.server.work_queue.provider:') for hit in hits),(code,report)
    print(json.dumps({'owner_heldout_seed':'forbidden analytical import','observed_exit':code,'classification':report['classification'],'expected_rejection':True}))
"""
commands.append(["-c", seed_code])
results = []
for i, args in enumerate(commands):
    r = subprocess.run(
        [a.python, *args], cwd=root, env=env, capture_output=True, text=True, timeout=50
    )
    log = r.stdout + r.stderr
    (out / f"command-{i}.log").write_text(log)
    results.append(
        {
            "argv": args,
            "returncode": r.returncode,
            "log_sha256": hashlib.sha256(log.encode()).hexdigest(),
        }
    )
    if r.returncode:
        break
counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
if (out / "results.xml").exists():
    for suite in ET.parse(out / "results.xml").getroot().iter("testsuite"):
        for k in counts:
            counts[k] += int(suite.get(k, 0))
passed = (
    len(results) == 3
    and all(x["returncode"] == 0 for x in results)
    and counts["tests"] >= 5
    and not any(counts[k] for k in ("failures", "errors", "skipped"))
)
receipt = {
    "source": PIN,
    "lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
    "interpreter": subprocess.check_output(
        [a.python, "-c", "import sys;print(sys.executable);print(sys.version)"],
        env=env,
        text=True,
    ).strip(),
    "commands": results,
    "counts": counts,
    "passed": passed,
    "scope": "isolated structural/quota behavior; not live GitHub acceptance or profile installation",
}
(out / "proof.json").write_text(json.dumps(receipt, indent=2))
print(json.dumps(receipt, indent=2))
sys.exit(0 if passed else 1)
