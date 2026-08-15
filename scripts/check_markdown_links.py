#!/usr/bin/env python3
"""Check local inline Markdown links without fetching external URLs."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

INLINE_LINK = re.compile(r"!?\[[^]]*]\((?P<target><[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")


def markdown_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.md")))
        elif path.suffix.lower() == ".md":
            files.append(path)
    return files


def local_target(target: str) -> str | None:
    target = target.strip("<>")
    if not target or target.startswith("#"):
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    return parsed.path


def find_broken_links(paths: list[Path]) -> list[tuple[Path, int, str]]:
    broken: list[tuple[Path, int, str]] = []
    for source in markdown_files(paths):
        in_fence = False
        for line_number, line in enumerate(
            source.read_text(errors="replace").splitlines(), 1
        ):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for match in INLINE_LINK.finditer(line):
                target = local_target(match.group("target"))
                if target is None:
                    continue
                if not (source.parent / target).exists():
                    broken.append((source, line_number, target))
    return broken


def main(argv: list[str] | None = None) -> int:
    paths = [Path(arg) for arg in (argv or sys.argv[1:])]
    if not paths:
        print("usage: check_markdown_links.py PATH [PATH ...]", file=sys.stderr)
        return 2
    broken = find_broken_links(paths)
    for source, line_number, target in broken:
        print(f"{source}:{line_number} -> {target}")
    print(f"Markdown link check: {len(broken)} broken link(s)")
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
