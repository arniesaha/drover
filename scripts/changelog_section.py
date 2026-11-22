#!/usr/bin/env python3
"""Print one version's CHANGELOG.md section, for curated release notes.

The Release workflow passes this to ``gh release create --notes-file`` ahead
of GitHub's generated PR list, so a release page leads with what changed and
its known limitations instead of only a list of merge titles. A version with
no section, or an empty one, fails: ``make check-release-ready`` already
requires the section to exist before tagging.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def changelog_section(text: str, version: str) -> str:
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*$", re.MULTILINE)
    match = heading.search(text)
    if match is None:
        raise ValueError(f"CHANGELOG.md has no '## [{version}]' section")
    following = re.compile(r"^## \[", re.MULTILINE).search(text, match.end())
    body = text[match.end() : following.start() if following else len(text)]
    # Drop trailing link reference definitions that belong to the whole file.
    body = re.sub(r"(?m)^\[[^\]]+\]:\s+\S+\s*$", "", body).strip()
    if not body:
        raise ValueError(f"CHANGELOG.md section for {version} is empty")
    return body + "\n"


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print("usage: changelog_section.py VERSION [CHANGELOG.md]", file=sys.stderr)
        return 2
    version = argv[1].removeprefix("v")
    path = Path(argv[2] if len(argv) == 3 else "CHANGELOG.md")
    try:
        sys.stdout.write(changelog_section(path.read_text(), version))
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
