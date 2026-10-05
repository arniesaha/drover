#!/usr/bin/env python3
"""Run ``drover-server gate`` end to end against scratch resources only.

Creates two disposable PostgreSQL clusters under /tmp on spare ports: a
"source" restored from the committed production control-store fixture
(tests/acceptance/fixtures), and a separate scratch cluster for the gate's
copies. The legacy root is the deterministic synthetic acceptance lake. No
production config, DSN, service, lake or incoming directory is read.

    uv run python scripts/lake_gate_rehearsal.py --extensions ~/.cache/drover-lake-ext

Prints the gate's JSON verdict and exits with the gate's exit code.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "acceptance"))


def _bindir() -> Path:
    for candidate in (
        os.environ.get("DROVER_TEST_PG_BINDIR"),
        str(Path(shutil.which("initdb") or "/nonexistent").parent),
        "/opt/homebrew/opt/postgresql@17/bin",
    ):
        if candidate and (Path(candidate) / "initdb").exists():
            return Path(candidate)
    raise SystemExit("initdb not found; set DROVER_TEST_PG_BINDIR")


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def scratch_cluster(bindir: Path, label: str):
    """A private cluster listening only on a Unix socket in a temp directory."""
    root = Path(tempfile.mkdtemp(prefix=f"drvgate{label}", dir="/tmp"))
    data, port = root / "data", _port()
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    subprocess.run(
        [str(bindir / "initdb"), "-D", str(data), "-A", "trust"]
        + ["-U", getpass.getuser(), "--no-sync", "-E", "UTF8"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        [str(bindir / "pg_ctl"), "-D", str(data), "-w", "-l", str(root / "log")]
        + ["-o", f"-k {root} -p {port} -c listen_addresses='' -c fsync=off"]
        + ["start"],
        check=True,
        capture_output=True,
        env=env,
    )
    try:
        yield f"host={root} port={port} dbname=postgres user={getpass.getuser()}"
    finally:
        subprocess.run(
            [str(bindir / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            capture_output=True,
            env=env,
        )
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--extensions", type=Path, required=True)
    parser.add_argument("--lake", default="rehearsal")
    parser.add_argument("--cache", type=Path, default=Path("/tmp/drover-gate-cache"))
    parser.add_argument("--memory-limit", default="4GB")
    parser.add_argument("--keep-dir", action="store_true")
    args = parser.parse_args()

    import _duckdb
    import psycopg
    from conftest import restore_prod_control_schema
    from lake_generator import get_cached_lake
    from psycopg.conninfo import make_conninfo

    bindir = _bindir()
    legacy = get_cached_lake(scale="small", seed=42, cache_dir=args.cache)
    with ExitStack() as stack:
        source_admin = stack.enter_context(scratch_cluster(bindir, "src"))
        scratch_admin = stack.enter_context(scratch_cluster(bindir, "dst"))
        with psycopg.connect(source_admin, autocommit=True) as con:
            con.execute("CREATE DATABASE drover_rehearsal_source")
        source = make_conninfo(source_admin, dbname="drover_rehearsal_source")
        restore_prod_control_schema(source)
        lake_root = Path(tempfile.mkdtemp(prefix="drover-gate-lakes-"))
        env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "REHEARSAL_SOURCE_DSN": source,
            "REHEARSAL_SCRATCH_DSN": scratch_admin,
            "DROVER_LAKE_EXTENSION_DIR": str(args.extensions.expanduser()),
            # Rehearsal only: production supplies the installer-verified digest.
            "DROVER_LAKE_ENGINE_SHA256": hashlib.sha256(
                Path(_duckdb.__file__).read_bytes()
            ).hexdigest(),
        }
        result = subprocess.run(
            [sys.executable, "-m", "drover.server", "gate"]
            + ["--lake", args.lake, "--lake-root", str(lake_root)]
            + ["--legacy-root", str(legacy)]
            + ["--source-dsn-env", "REHEARSAL_SOURCE_DSN"]
            + ["--scratch-admin-dsn-env", "REHEARSAL_SCRATCH_DSN"]
            + ["--memory-limit", args.memory_limit, "--pg-bin", str(bindir)],
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        sys.stdout.write(result.stdout)
        print(f"gate exit code: {result.returncode}", file=sys.stderr)
        print(f"gate directory: {lake_root}", file=sys.stderr)
        if not args.keep_dir and result.returncode == 0:
            shutil.rmtree(lake_root, ignore_errors=True)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
