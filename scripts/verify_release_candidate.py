#!/usr/bin/env python3
"""Verify the PostgreSQL-default contract in an installed release runtime.

The release workflow invokes this only after it has installed the built wheel
and hash-pinned dependency lock into an isolated virtual environment. Keeping
the check runtime-based prevents source and release artifacts from drifting.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence

CONTRACT_CHECK = r"""
import inspect
from dataclasses import fields

from drover.config import DroverConfig
from drover.server.service_units import render_launchd, render_systemd

if "control_store" not in {field.name for field in fields(DroverConfig)}:
    raise SystemExit("DroverConfig has no control_store field")
for renderer in (render_launchd, render_systemd):
    if "environment_file" not in inspect.signature(renderer).parameters:
        raise SystemExit(f"{renderer.__name__} has no environment_file parameter")
"""


def verify_runtime(runtime_python: Path, runtime_server: Path) -> None:
    """Raise CalledProcessError when an installed candidate lacks the contract."""
    subprocess.run([str(runtime_python), "-c", CONTRACT_CHECK], check=True)
    subprocess.run(
        [str(runtime_server), "control-store", "init", "--help"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True, dest="runtime_python")
    parser.add_argument("--server", type=Path, required=True, dest="runtime_server")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        verify_runtime(args.runtime_python, args.runtime_server)
    except (OSError, subprocess.CalledProcessError):
        print(
            "candidate runtime lacks the PostgreSQL-default installer contract",
            file=sys.stderr,
        )
        return 1
    print("candidate runtime satisfies the PostgreSQL-default installer contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
