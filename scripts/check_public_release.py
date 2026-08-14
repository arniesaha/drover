#!/usr/bin/env python3
"""Audit tracked text files for values that should not ship publicly."""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Pattern, Sequence


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    excerpt: str


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: Pattern[str]


RULES = (
    Rule(
        "personal-home-path",
        re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/"),
    ),
    Rule(
        "private-ip-address",
        re.compile(
            r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
            r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
        ),
    ),
    Rule(
        "private-tailnet-hostname",
        re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net\b", re.IGNORECASE),
    ),
    Rule(
        "private-roadmap-link",
        re.compile(
            r"https://github\.com/arniesaha/(?:nexus|drover-roadmap)\b",
            re.IGNORECASE,
        ),
    ),
    Rule("legacy-public-name", re.compile(r"\bNexusKit\b")),
    Rule("legacy-harness-name", re.compile(r"\bMeta Harness\b")),
)

ENVIRONMENT_RULES = frozenset(
    {
        "personal-home-path",
        "private-ip-address",
        "private-tailnet-hostname",
        "private-roadmap-link",
    }
)
RELEASE_FACING_PARTS = frozenset({"docs", "deploy", "scripts", ".github"})
RELEASE_FACING_SUFFIXES = frozenset({".env", ".md", ".plist", ".toml", ".yaml", ".yml"})

CREDENTIAL_PATTERN = re.compile(
    r"(?i)(?<![\"'])\b(?:api[_-]?key|password|secret|token)\b"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]+\"|'[^'\r\n]+')"
)
LEGACY_POSITIONING_PATTERN = re.compile(
    r"\bformerly\b[^\r\n]{0,80}(?<![./])\bNexus\b(?!\.(?:\*|[A-Za-z0-9_]))",
    re.IGNORECASE,
)

# Only the named rule is suppressed. Other findings in an allowlisted file still fail.
RULE_ALLOWLIST = {
    "docs/compatibility.md": frozenset({"legacy-public-name"}),
    "scripts/check_public_release.py": frozenset(
        {"legacy-public-name", "private-roadmap-link"}
    ),
    "tests/test_check_public_release.py": frozenset(
        {
            "credential-value",
            "legacy-public-name",
            "legacy-harness-name",
            "personal-home-path",
            "private-ip-address",
            "private-tailnet-hostname",
            "private-roadmap-link",
        }
    ),
}

RULE_PREFIX_ALLOWLIST = {
    "credential-value": ("tests/", "apps/drover/DroverKit/Tests/"),
    "legacy-harness-name": ("tests/", "apps/drover/DroverKit/Tests/"),
}


def _allowlisted(path: Path, rule: str) -> bool:
    normalized = path.as_posix()
    exact_match = any(
        (normalized == suffix or normalized.endswith(f"/{suffix}"))
        and rule in allowed_rules
        for suffix, allowed_rules in RULE_ALLOWLIST.items()
    )
    prefix_match = any(
        normalized.startswith(prefix) or f"/{prefix}" in normalized
        for prefix in RULE_PREFIX_ALLOWLIST.get(rule, ())
    )
    return exact_match or prefix_match


def _redact_credential(match: re.Match[str]) -> str:
    return (
        match.string[: match.start("value")]
        + "[REDACTED]"
        + match.string[match.end("value") :]
    ).strip()


def _is_release_facing(path: Path) -> bool:
    return (
        bool(set(path.parts) & RELEASE_FACING_PARTS)
        or "src" in path.parts
        or path.name.lower() == "readme.md"
        or path.suffix.lower() in RELEASE_FACING_SUFFIXES
    )


def _is_public_prose_path(path: Path) -> bool:
    parts = path.parts
    normalized = path.as_posix()
    if path.suffix.lower() != ".md":
        return False
    if "tests" in parts or "fixtures" in parts or "snapshots" in parts:
        return False
    if "/src/drover/prompts/" in f"/{normalized.lstrip('/')}":
        return False
    if path.name.lower() == "readme.md":
        return True
    return "docs" in parts or "skills" in parts


def check_paths(paths: Sequence[Path], *, root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        scope_path = path.relative_to(root)
        normalized = f"/{scope_path.as_posix().lstrip('/')}"
        if (
            normalized.endswith("/docs/roadmap.md")
            or "/docs/superpowers/" in normalized
        ):
            findings.append(
                Finding(
                    path=str(path),
                    line=1,
                    rule="private-planning-path",
                    excerpt=path.as_posix(),
                )
            )

        if (
            scope_path.name == "SKILL.md"
            and scope_path.parent.name == "nexus"
            and scope_path.parent.parent.name == "skills"
        ):
            findings.append(
                Finding(
                    path=str(path),
                    line=1,
                    rule="legacy-skill-entrypoint",
                    excerpt="skills/nexus/SKILL.md",
                )
            )

        try:
            data = path.read_bytes()
        except (OSError, ValueError):
            continue
        if b"\0" in data:
            continue

        text = data.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            credential = CREDENTIAL_PATTERN.search(line)
            if credential and not _allowlisted(scope_path, "credential-value"):
                findings.append(
                    Finding(
                        path=str(path),
                        line=line_number,
                        rule="credential-value",
                        excerpt=_redact_credential(credential),
                    )
                )

            if _is_public_prose_path(scope_path):
                if "—" in line:
                    findings.append(
                        Finding(
                            path=str(path),
                            line=line_number,
                            rule="public-em-dash",
                            excerpt=line.strip(),
                        )
                    )
                if LEGACY_POSITIONING_PATTERN.search(line):
                    findings.append(
                        Finding(
                            path=str(path),
                            line=line_number,
                            rule="legacy-positioning-copy",
                            excerpt=line.strip(),
                        )
                    )

            for rule in RULES:
                if rule.name in ENVIRONMENT_RULES and not _is_release_facing(
                    scope_path
                ):
                    continue
                if rule.pattern.search(line) and not _allowlisted(
                    scope_path, rule.name
                ):
                    findings.append(
                        Finding(
                            path=str(path),
                            line=line_number,
                            rule=rule.name,
                            excerpt=line.strip(),
                        )
                    )
    return findings


def _tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / name.decode() for name in result.stdout.split(b"\0") if name]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=(
            "Files to audit. Defaults to every tracked file, which is what CI "
            "checks; pass paths explicitly to audit a set git does not track "
            "yet, such as the staged contents of a commit."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=(
            "Repository root the paths are scoped against. Every rule keyed on "
            "location, including the allowlists, is evaluated relative to it. "
            "Defaults to the repository this script lives in."
        ),
    )
    return parser.parse_args(argv)


def _resolve_within(paths: Sequence[Path], root: Path) -> list[Path] | None:
    resolved: list[Path] = []
    for raw in paths:
        path = raw.resolve()
        try:
            path.relative_to(root)
        except ValueError:
            print(f"{raw}: outside the audit root {root}")
            return None
        resolved.append(path)
    return resolved


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    root = (args.root or Path(__file__).resolve().parents[1]).resolve()
    if args.paths:
        paths = _resolve_within(args.paths, root)
        if paths is None:
            return 2
    else:
        paths = _tracked_paths(root)
    findings = check_paths(paths, root=root)
    for finding in findings:
        path = Path(finding.path)
        try:
            display_path = path.relative_to(root)
        except ValueError:
            display_path = path
        print(f"{display_path}:{finding.line}: {finding.rule}")
    print(f"Public release audit: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
