#!/usr/bin/env python3
"""Bounded cleanup for a trusted self-hosted runner job."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO


class CleanupError(RuntimeError):
    """Raised when a runner cleanup target is outside its safety boundary."""


def _resolve_path(raw: str | None, label: str) -> Path:
    if raw is None or not raw.strip():
        raise CleanupError(f"{label} is missing")
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CleanupError(f"{label} is invalid") from exc


def _validated_root(raw: str | None) -> Path:
    root = _resolve_path(raw, "work root")
    if root.parent == root:
        raise CleanupError("work root cannot be the filesystem root")
    try:
        home = Path.home().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise CleanupError("current user's home directory is unavailable") from exc
    if root == home:
        raise CleanupError("work root cannot be the current user's home directory")
    return root


def _validated_child(root: Path, raw: str | None, expected_name: str) -> Path:
    target = _resolve_path(raw, expected_name)
    if target == root or root not in target.parents:
        raise CleanupError(f"{expected_name} is outside the configured work root")
    if target.name != expected_name:
        raise CleanupError(f"{expected_name} has an unexpected path component")
    if target.exists() and not target.is_dir():
        raise CleanupError(f"{expected_name} is not a directory")
    return target


def cleanup_job(environ: Mapping[str, str]) -> tuple[Path, ...]:
    """Remove the job checkout and temporary directory within the runner root."""

    root = _validated_root(environ.get("DROVER_RUNNER_WORK_ROOT"))
    workspace = _validated_child(root, environ.get("GITHUB_WORKSPACE"), "drover")
    temp_dir = _validated_child(root, environ.get("RUNNER_TEMP"), "_temp")

    removed: list[Path] = []
    for target in (workspace, temp_dir):
        if target.exists():
            shutil.rmtree(target)
            removed.append(target)
    return tuple(removed)


def main(environ: Mapping[str, str] | None = None, stderr: TextIO | None = None) -> int:
    environ = os.environ if environ is None else environ
    stderr = sys.stderr if stderr is None else stderr
    try:
        cleanup_job(environ)
    except CleanupError as exc:
        print(f"post-job cleanup rejected: {exc}", file=stderr)
        return 1
    except OSError:
        print("post-job cleanup failed: target could not be removed", file=stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
