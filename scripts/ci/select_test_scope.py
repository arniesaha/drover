#!/usr/bin/env python3
"""Select the public CI suites required by a changed-path set.

The classifier is deliberately fail-closed: only README and documentation
paths avoid runtime suites, and any unrecognised path runs the Python gate.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class TestScope:
    python: bool
    ios: bool


def select_scope(paths: Iterable[str]) -> TestScope:
    normalized = tuple(_normalize(path) for path in paths if _normalize(path))
    if not normalized:
        return TestScope(python=True, ios=True)
    return TestScope(
        python=any(_requires_python(path) for path in normalized),
        ios=any(_requires_ios(path) for path in normalized),
    )


def changed_paths(base: str, head: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(result.stdout.splitlines())


def _normalize(path: str) -> str:
    normalized = path.strip()
    return normalized[2:] if normalized.startswith("./") else normalized


def _requires_python(path: str) -> bool:
    if _is_presentation_path(path):
        return False
    if _requires_ios(path) and path != ".github/workflows/ios.yml":
        return False
    return True


def _requires_ios(path: str) -> bool:
    return path.startswith("apps/drover/") or path == ".github/workflows/ios.yml"


def _is_presentation_path(path: str) -> bool:
    return path == "README.md" or path.startswith("docs/")


def _write_github_output(path: Path, scope: TestScope) -> None:
    with path.open("a", encoding="utf-8") as output:
        for name, value in asdict(scope).items():
            output.write(f"{name}={'true' if value else 'false'}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    paths = changed_paths(args.base, args.head) if args.base else args.paths
    scope = select_scope(paths)
    if args.github_output is not None:
        _write_github_output(args.github_output, scope)
    print(json.dumps(asdict(scope), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
