#!/usr/bin/env python3
"""Regenerate normalized parser goldens from recorded structured NDJSON.

Only generated message ids and timestamps are masked.  The remaining fields
are the compatibility contract a non-Python harnessd implementation consumes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from drover.server.harness.structured.claude import ClaudeDriver
from drover.server.harness.structured.codex import CodexDriver

FIXTURES = Path("tests/fixtures/structured")


def _driver(path: Path):
    if path.name.startswith("claude_"):
        return ClaudeDriver(["true"], None, lambda _message: None)
    if path.name.startswith("codex_"):
        return CodexDriver(["true"], None, lambda _message: None)
    return None


def _normalize(message: object) -> dict:
    value = message.to_payload()
    value["event_id"] = "<event_id>"
    value["ts"] = "<ts>"
    return value


def generate(path: Path) -> list[dict]:
    driver = _driver(path)
    if driver is None:
        return []
    output = []
    for line in path.read_text().splitlines():
        if not line:
            continue
        messages = (
            driver.parse_line(line)
            if isinstance(driver, ClaudeDriver)
            else driver.parse_line(line)
        )
        output.extend(_normalize(message) for message in messages)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for fixture in sorted(FIXTURES.glob("*.ndjson")):
        if _driver(fixture) is None:
            continue
        expected = fixture.with_suffix(".expected.jsonl")
        rendered = "".join(
            json.dumps(row, sort_keys=True) + "\n" for row in generate(fixture)
        )
        if args.check:
            if not expected.exists() or expected.read_text() != rendered:
                stale.append(str(expected))
        else:
            expected.write_text(rendered)
    if stale:
        raise SystemExit("stale parser goldens: " + ", ".join(stale))


if __name__ == "__main__":
    main()
