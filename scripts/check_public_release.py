#!/usr/bin/env python3
"""Audit tracked text files for values that should not ship publicly."""

from __future__ import annotations

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
            "personal-home-path",
            "private-ip-address",
            "private-tailnet-hostname",
            "private-roadmap-link",
        }
    ),
}

RULE_PREFIX_ALLOWLIST = {
    "credential-value": ("tests/", "apps/drover/DroverKit/Tests/"),
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
        or path.name.lower() == "readme.md"
        or path.suffix.lower() in RELEASE_FACING_SUFFIXES
    )


def check_paths(paths: Sequence[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        normalized = f"/{path.as_posix().lstrip('/')}"
        if normalized.endswith("/docs/roadmap.md") or "/docs/superpowers/" in normalized:
            findings.append(
                Finding(
                    path=str(path),
                    line=1,
                    rule="private-planning-path",
                    excerpt=path.as_posix(),
                )
            )

        if (
            path.name == "SKILL.md"
            and path.parent.name == "nexus"
            and path.parent.parent.name == "skills"
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
            if credential and not _allowlisted(path, "credential-value"):
                findings.append(
                    Finding(
                        path=str(path),
                        line=line_number,
                        rule="credential-value",
                        excerpt=_redact_credential(credential),
                    )
                )

            for rule in RULES:
                if rule.name in ENVIRONMENT_RULES and not _is_release_facing(path):
                    continue
                if rule.pattern.search(line) and not _allowlisted(path, rule.name):
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


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings = check_paths(_tracked_paths(root))
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
