"""Profile-scoped developer/verification environment preflight (stdlib only).

Run through ``scripts/quality/preflight.sh`` so an absent interpreter is
classified too. Contract: docs/operations/dev-preflight.md. Exit codes:
0 pass, 1 product_test_failure (only from ``--then``), 3 environment_unavailable,
4 harness_failure. The ``--then`` argv runs only after every check passed.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

PASS, PRODUCT, ENV, HARNESS = (
    "pass",
    "product_test_failure",
    "environment_unavailable",
    "harness_failure",
)
EXIT = {PASS: 0, PRODUCT: 1, ENV: 3, HARNESS: 4}
PROFILES = {
    "focused-tests": {"tools": ("git",), "modules": ("pytest", "drover"), "pg": False},
    "focused-tests-pg": {
        "tools": ("git",),
        "modules": ("pytest", "drover", "psycopg"),
        "pg": True,
    },
}
SHARED_TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/tmp")
# "pg" is None for non-PG profiles; for the PG profile it binds endpoint,
# dbname, role, server major, client tools and data directory.
RECEIPT_KEYS = (
    "profile",
    "host",
    "user",
    "worktree",
    "head",
    "lock_sha256",
    "interpreter",
    "python_version",
    "pg",
)
SYNC_ACTION = "Run `uv sync --locked --extra dev` in this worktree, or pass --python."
_remove = os.unlink  # indirection so tests can induce a cleanup failure

# Executed by the resolved interpreter in isolated mode. Module names come from
# the fixed PROFILES table, never from caller, issue or report text.
_PROBE = r"""
import importlib, json, sys
out = {"executable": sys.executable, "version": list(sys.version_info[:3]), "modules": {}}
for name in sys.argv[1:]:
    try:
        mod = importlib.import_module(name)
        out["modules"][name] = {"ok": True, "file": getattr(mod, "__file__", None),
                                "version": getattr(mod, "__version__", None)}
    except Exception as exc:
        out["modules"][name] = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:200]}
print(json.dumps(out))
"""

# Read-only: no DDL/DML, transaction forced READ ONLY. The DSN stays in the
# environment variable the caller named; it is never printed.
_PG_PROBE = r"""
import json, os, sys
import psycopg
with psycopg.connect(os.environ[sys.argv[1]], connect_timeout=5) as con:
    con.execute("SET TRANSACTION READ ONLY")
    row = con.execute(
        "SELECT current_user, current_setting('server_version_num')::int,"
        " current_setting('transaction_read_only'), (SELECT count(*) FROM pg_namespace),"
        " CASE WHEN pg_has_role(current_user, 'pg_read_all_settings', 'USAGE')"
        " THEN current_setting('data_directory') END"
    ).fetchone()
    print(json.dumps({"user": row[0], "server_version_num": row[1], "read_only": row[2],
                      "namespaces": row[3], "data_directory": row[4],
                      "host": con.info.host, "port": con.info.port,
                      "dbname": con.info.dbname}))
"""


class Failure(Exception):
    def __init__(self, classification: str, check: str, message: str, action: str = ""):
        super().__init__(message)
        self.classification, self.check, self.message, self.action = (
            classification,
            check,
            message,
            action,
        )


def _run(argv, *, cwd=None, timeout=30):
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def check_worktree(root: Path, args, git: str) -> dict:
    top = _run([git, "rev-parse", "--show-toplevel"], cwd=root)
    if top.returncode != 0:
        raise Failure(ENV, "worktree", f"{root} is not a git checkout")
    actual = Path(top.stdout.strip()).resolve()
    if args.expect_worktree and actual != Path(args.expect_worktree).resolve():
        raise Failure(
            ENV,
            "worktree",
            f"checkout is {actual}, expected " f"{args.expect_worktree}",
            "Run from the allocated worktree.",
        )
    head = _run([git, "rev-parse", "HEAD"], cwd=actual).stdout.strip()
    if (
        args.expect_base
        and _run(
            [git, "merge-base", "--is-ancestor", args.expect_base, "HEAD"], cwd=actual
        ).returncode
        != 0
    ):
        raise Failure(
            ENV,
            "base",
            f"HEAD {head[:12]} does not contain {args.expect_base}",
            "Recreate or rebase the worktree on the intended base.",
        )
    dirty = bool(
        _run(
            [git, "status", "--porcelain", "--untracked-files=no"], cwd=actual
        ).stdout.strip()
    )
    if args.require_clean and dirty:
        raise Failure(
            ENV, "clean", "tracked files are modified", "Commit or discard first."
        )
    return {
        "worktree": str(actual),
        "head": head,
        "base": args.expect_base,
        "dirty": dirty,
    }


def check_inputs(root: Path) -> tuple[str, str]:
    for rel in ("pyproject.toml", "uv.lock"):
        if not os.access(root / rel, os.R_OK):
            raise Failure(ENV, "readable_input", f"{rel} is not readable in {root}")
    meta = tomllib.loads((root / "pyproject.toml").read_text())
    lock = hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
    return meta.get("project", {}).get("requires-python", ""), lock


def check_output(out: Path, stack: contextlib.ExitStack) -> str:
    out = out.expanduser().resolve()
    for shared in SHARED_TEMP_ROOTS:
        shared_path = Path(shared).resolve()
        if out == shared_path or shared_path in out.parents:
            raise Failure(
                ENV,
                "output_dir",
                f"{out} is under shared temp {shared}",
                "Use an isolated directory such as ~/scratch/<run>.",
            )
    if not out.is_dir():
        raise Failure(
            ENV,
            "output_dir",
            f"{out} is not an existing directory",
            "Create the isolated output directory; the helper never does.",
        )
    try:
        fd, probe = tempfile.mkstemp(prefix=".preflight-probe-", dir=out)
    except OSError as exc:
        raise Failure(
            ENV, "output_dir", f"{out} is not writable: {exc.strerror}"
        ) from exc
    # The only resource this run creates before the receipt; always removed.
    stack.callback(lambda: _cleanup(probe))
    with os.fdopen(fd, "w") as handle:
        handle.write("ok")
    if Path(probe).read_text() != "ok":
        raise Failure(ENV, "output_dir", f"{out} did not read back the probe")
    return str(out)


def _cleanup(probe: str) -> None:
    try:
        _remove(probe)
    except OSError as exc:
        raise Failure(
            HARNESS,
            "cleanup",
            f"could not remove own probe {probe} "
            f"({exc.strerror}); left in place as proof",
        ) from exc


def _meets(version: list[int], requires: str) -> bool:
    spec = requires.replace(" ", "").split(",")[0]
    if not spec.startswith(">="):
        return True
    want = tuple(int(p) for p in spec[2:].split(".") if p.isdigit())
    return tuple(version[: len(want)]) >= want


def check_interpreter(
    root: Path, python: str | None, modules, requires: str, path: str
) -> dict:
    candidate = python or str(root / ".venv" / "bin" / "python")
    if os.sep not in candidate:
        candidate = shutil.which(candidate, path=path) or candidate
    if not os.access(candidate, os.X_OK):
        raise Failure(
            ENV, "interpreter", f"no executable interpreter at {candidate}", SYNC_ACTION
        )
    probe = _run([candidate, "-I", "-c", _PROBE, *modules], cwd=root)
    try:
        info = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise Failure(
            ENV,
            "interpreter",
            f"{candidate} did not run the probe "
            f"(exit {probe.returncode}): {probe.stderr[-200:]}",
        ) from exc
    version = ".".join(map(str, info["version"]))
    if not _meets(info["version"], requires):
        raise Failure(
            ENV,
            "interpreter",
            f"{info['executable']} is {version}; project " f"requires {requires}",
            SYNC_ACTION,
        )
    for name, mod in info["modules"].items():
        if not mod["ok"]:
            raise Failure(
                ENV,
                f"import:{name}",
                f"{name} not importable with " f"{info['executable']}: {mod['error']}",
                SYNC_ACTION + " Never pip-install globally.",
            )
    drover_file = (info["modules"].get("drover") or {}).get("file")
    src = (root / "src").resolve()
    if drover_file and src not in Path(drover_file).resolve().parents:
        raise Failure(
            ENV,
            "import:drover",
            f"drover resolves to {drover_file}, not " f"{src}",
            "Use this worktree's .venv, not an installed runtime.",
        )
    return {
        "interpreter": info["executable"],
        "python_version": version,
        "modules": {k: v.get("version") for k, v in info["modules"].items()},
    }


def check_postgres(interpreter: str, args, path: str) -> dict:
    if not args.pg_dsn_env or not os.environ.get(args.pg_dsn_env):
        raise Failure(
            ENV,
            "pg:dsn",
            f"profile needs --pg-dsn-env naming a set variable "
            f"(got {args.pg_dsn_env!r})",
            "Point it at an isolated test fixture.",
        )
    tool = shutil.which("psql", path=path)
    if tool is None:
        raise Failure(
            ENV,
            "pg:binary",
            "psql is not on PATH; the PG profile needs client "
            "tools matching the server major",
            "Put the fixture's PostgreSQL bindir first on PATH.",
        )
    version_out = _run([tool, "--version"])
    # e.g. "psql (PostgreSQL) 17.4" or "psql (PostgreSQL) 17.4 (Homebrew)"
    match = re.search(r"\(PostgreSQL\)\s+((\d+)(?:\.\d+)*)", version_out.stdout)
    if version_out.returncode != 0 or match is None:
        raise Failure(
            ENV,
            "pg:binary",
            f"cannot parse `{tool} --version` (exit " f"{version_out.returncode})",
            "Use a real PostgreSQL psql binary.",
        )
    client = match.group(1)
    try:
        probe = _run([interpreter, "-I", "-c", _PG_PROBE, args.pg_dsn_env], timeout=20)
    except subprocess.TimeoutExpired as exc:
        raise Failure(ENV, "pg:query", "read-only fixture query timed out") from exc
    if probe.returncode != 0:
        last = (probe.stderr.strip().splitlines() or ["?"])[-1][:200]
        raise Failure(ENV, "pg:query", f"read-only fixture query failed: {last}")
    info = json.loads(probe.stdout)
    endpoint = f"{info['host']}:{info['port']}"
    forbidden = set(args.pg_forbid_endpoint or ())
    if endpoint in forbidden or str(info["port"]) in forbidden:
        raise Failure(
            ENV,
            "pg:endpoint",
            f"DSN reaches forbidden endpoint {endpoint}",
            "Never point tests at a shared or production cluster.",
        )
    if args.pg_expect_role and info["user"] != args.pg_expect_role:
        raise Failure(
            ENV,
            "pg:role",
            f"connected as {info['user']!r}, expected " f"{args.pg_expect_role!r}",
        )
    data = info.get("data_directory")
    if args.pg_expect_data_prefix and not (data or "").startswith(
        args.pg_expect_data_prefix
    ):
        raise Failure(
            ENV,
            "pg:data_directory",
            f"fixture data directory is {data!r}, "
            f"not under {args.pg_expect_data_prefix}",
        )
    major = info["server_version_num"] // 10000
    if args.pg_expect_major and major != args.pg_expect_major:
        raise Failure(
            ENV,
            "pg:version",
            f"server major {major}, expected " f"{args.pg_expect_major}",
        )
    if int(match.group(2)) != major:
        raise Failure(
            ENV,
            "pg:binary",
            f"psql on PATH is {client}, server major is " f"{major}",
            "Put the matching PostgreSQL bindir first on PATH.",
        )
    if info["read_only"] != "on":
        raise Failure(HARNESS, "pg:query", "probe transaction was not read-only")
    return {
        "pg": {
            "endpoint": endpoint,
            "dbname": info["dbname"],
            "user": info["user"],
            "server_major": major,
            "client_tools": client,
            "data_directory": data,
        }
    }


def run_checks(args, stack: contextlib.ExitStack, path: str) -> dict:
    profile = PROFILES[args.profile]
    root = Path(args.root).resolve()
    tools = {}
    for tool in profile["tools"]:
        tools[tool] = shutil.which(tool, path=path)
        if tools[tool] is None:
            raise Failure(ENV, f"tool:{tool}", f"{tool} is not on PATH")
    receipt = {
        "profile": args.profile,
        "host": socket.gethostname(),
        "user": getpass.getuser(),
        "platform": platform.platform(),
        "run_id": uuid.uuid4().hex,
        "created_at": int(time.time()),
    }
    receipt.update(check_worktree(root, args, tools["git"]))
    requires, receipt["lock_sha256"] = check_inputs(root)
    receipt["output_dir"] = check_output(Path(args.out), stack)
    receipt.update(
        check_interpreter(root, args.python, profile["modules"], requires, path)
    )
    uv = shutil.which("uv", path=path)
    receipt["uv"] = _run([uv, "--version"]).stdout.strip() if uv else None
    if profile["pg"]:
        receipt.update(check_postgres(receipt["interpreter"], args, path))
    return receipt


def verify_receipt(receipt_path: Path, current: dict, max_age: int) -> None:
    try:
        old = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise Failure(ENV, "receipt", f"cannot read receipt {receipt_path}") from exc
    stale = [k for k in RECEIPT_KEYS if old.get(k) != current.get(k)]
    if stale:
        raise Failure(
            ENV, "receipt", f"receipt does not certify this run; differs in {stale}"
        )
    if current["created_at"] - int(old.get("created_at", 0)) > max_age:
        raise Failure(ENV, "receipt", f"receipt is older than {max_age}s")


def run_then(argv: list[str], receipt: dict) -> dict:
    if argv[0] in ("python", "python3"):
        raise Failure(HARNESS, "then", "use {python} so the verified interpreter runs")
    argv = [receipt["interpreter"] if a == "{python}" else a for a in argv]
    rc = subprocess.run(argv, cwd=receipt["worktree"], check=False).returncode
    if rc == 1:
        raise Failure(PRODUCT, "then", "focused command reported failures (exit 1)")
    if rc != 0:
        raise Failure(
            HARNESS, "then", f"command exited {rc} (usage/collection/internal)"
        )
    return {"argv0": Path(argv[0]).name, "returncode": rc}


def parse_args(argv: list[str]):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", choices=sorted(PROFILES), default="focused-tests")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--out", required=True, help="existing isolated output dir")
    parser.add_argument("--python", help="project interpreter (default <root>/.venv)")
    parser.add_argument("--expect-worktree")
    parser.add_argument("--expect-base")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--verify-receipt")
    parser.add_argument("--max-receipt-age", type=int, default=3600)
    parser.add_argument(
        "--pg-dsn-env", help="NAME of the env var holding the fixture DSN"
    )
    parser.add_argument("--pg-expect-role")
    parser.add_argument("--pg-expect-major", type=int)
    parser.add_argument("--pg-expect-data-prefix")
    parser.add_argument(
        "--pg-forbid-endpoint",
        action="append",
        help="host:port or bare port the fixture must not be",
    )
    then: list[str] = []
    if "--then" in argv:
        split = argv.index("--then")
        argv, then = argv[:split], argv[split + 1 :]
    args = parser.parse_args(argv)
    args.then = then
    return args


def main(argv=None, *, path: str | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    path = os.environ.get("PATH", "") if path is None else path
    result: dict = {"profile": args.profile}
    try:
        try:
            with contextlib.ExitStack() as stack:
                receipt = run_checks(args, stack, path)
                if args.verify_receipt:
                    verify_receipt(
                        Path(args.verify_receipt), receipt, args.max_receipt_age
                    )
        except OSError as exc:
            raise Failure(HARNESS, "io", f"{type(exc).__name__}: {exc}") from exc
        # Written only after our own probe was cleaned up, so a receipt exists
        # only for a fully passed, fully cleaned preflight.
        target = Path(receipt["output_dir"]) / f"preflight-{receipt['run_id']}.json"
        target.write_text(json.dumps(receipt, indent=2, sort_keys=True))
        result.update(classification=PASS, receipt=str(target))
        if args.then:
            result["then"] = run_then(args.then, receipt)
    except Failure as exc:
        result.update(
            classification=exc.classification,
            check=exc.check,
            message=exc.message,
            action=exc.action,
        )
    print(json.dumps(result, sort_keys=True))
    if result["classification"] != PASS:
        print(
            f"preflight {result['classification']}: [{result['check']}] "
            f"{result['message']} {result['action']}".rstrip(),
            file=sys.stderr,
        )
    return EXIT[result["classification"]]


if __name__ == "__main__":
    sys.exit(main())
