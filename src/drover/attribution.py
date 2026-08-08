"""Best-effort project/repository attribution for agent events.

Collectors run on the same host as the source harness, so this is the right
place to turn a raw ``cwd`` into stable repo metadata before the event is
shipped to the central context store.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from drover.task_id import parse_repo_url

# ---------------------------------------------------------------------------
# Optional known-roots mapping
# ---------------------------------------------------------------------------
# Events collected on remote hosts carry a ``cwd`` that is valid on *that*
# host but may not exist on the central context-store machine. The
# ``cwd_path.exists()`` guard below silently skips those paths. This optional
# mapping resolves configured remote project paths without a live ``git`` call.
#
# Keys are path *prefixes* (no trailing slash).  The longest matching prefix
# wins, so per-project subdirectories can be added later without ambiguity.
_KNOWN_ROOTS_ENV = "DROVER_REPO_ROOTS_JSON"
_GENERAL_WORKSPACE_ROOTS_ENV = "DROVER_GENERAL_WORKSPACE_ROOTS"
GENERAL_WORKSPACE_ACTIVITY_TYPE = "general_workspace"


def _configured_known_roots() -> dict[str, tuple[str, str]]:
    """Return operator-provided remote path attribution mappings.

    ``DROVER_REPO_ROOTS_JSON`` is a JSON object whose keys are absolute path
    prefixes and whose values are ``"owner/repo"`` strings. Invalid entries
    are ignored so a collector never stops processing because of optional
    attribution configuration.
    """
    raw = os.environ.get(_KNOWN_ROOTS_ENV, "").strip()
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(values, dict):
        return {}
    roots: dict[str, tuple[str, str]] = {}
    for prefix, repository in values.items():
        owner, name = _repo_from_repository_value(repository)
        if isinstance(prefix, str) and prefix.startswith("/") and owner and name:
            roots[prefix.rstrip("/") or "/"] = (owner, name)
    return roots


def configured_general_workspace_roots() -> frozenset[str]:
    """Return exact non-project workspace roots configured by the operator."""
    raw = os.environ.get(_GENERAL_WORKSPACE_ROOTS_ENV, "")
    return frozenset(
        str(Path(value).expanduser())
        for value in raw.split(os.pathsep)
        if value.strip()
    )


_OWNER_REPO_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
_WORKING_DIRECTORY_XML_RE = re.compile(
    r"<working_directory>\s*([^<]+?)\s*</working_directory>", re.IGNORECASE
)


def _known_root_match(cwd: str) -> Optional[tuple[str, str]]:
    """Return known-root attribution for exact roots or path descendants."""
    for prefix, attribution in sorted(
        _configured_known_roots().items(), key=lambda kv: len(kv[0]), reverse=True
    ):
        if cwd == prefix or cwd.startswith(f"{prefix}/"):
            return attribution
    return None


def classify_cwd_activity(cwd: Optional[str]) -> Optional[str]:
    """Classify known non-project workspace cwd values.

    This is deliberately narrower than repo attribution. It prevents generic
    home-directory activity from being reported as attribution breakage without
    turning broad parent directories into repo mappings.
    """
    if not cwd:
        return None
    normalized = str(Path(cwd).expanduser())
    if normalized in configured_general_workspace_roots():
        return GENERAL_WORKSPACE_ACTIVITY_TYPE
    return None


@dataclass(frozen=True)
class RepoAttribution:
    repo_owner: Optional[str] = None
    repo_name: Optional[str] = None
    branch: Optional[str] = None
    cwd: Optional[str] = None
    activity_type: Optional[str] = None


def _git(cwd: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def _cwd_from_raw(raw_data: dict[str, Any]) -> Optional[str]:
    for key in (
        "cwd",
        "currentWorkingDirectory",
        "working_directory",
        "workspaceDir",
        "prov.cwd",
    ):
        value = raw_data.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("content", "prompt", "text"):
        value = raw_data.get(key)
        if isinstance(value, str):
            match = _WORKING_DIRECTORY_XML_RE.search(value)
            if match:
                return html.unescape(match.group(1).strip())
    return None


def _repo_from_repository_value(value: Any) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    value = value.strip()
    owner, name = parse_repo_url(value)
    if owner and name:
        return owner, name
    match = _OWNER_REPO_RE.match(value.removesuffix(".git"))
    if match:
        return match.group(1), match.group(2)
    return None, None


def derive_repo_attribution(raw_data: Optional[dict[str, Any]]) -> RepoAttribution:
    """Return repo metadata already present in ``raw_data`` or inferred from cwd.

    Explicit fields win. Inference is intentionally conservative: it only calls
    git when ``cwd`` exists on this host. If central ingest sees a remote-only
    path, the event remains unattributed rather than guessing.
    """
    raw_data = raw_data or {}

    repo_owner = (
        raw_data.get("_repo_owner")
        or raw_data.get("repo_owner")
        or raw_data.get("prov.repo.owner")
    )
    repo_name = (
        raw_data.get("_repo_name")
        or raw_data.get("repo_name")
        or raw_data.get("prov.repo.name")
    )
    branch = (
        raw_data.get("gitBranch")
        or raw_data.get("git_branch")
        or raw_data.get("prov.git.branch")
        or raw_data.get("branch")
    )
    cwd = _cwd_from_raw(raw_data)

    if not (repo_owner and repo_name):
        for key in ("prov.repository", "repository", "repo", "repo_url"):
            owner, name = _repo_from_repository_value(raw_data.get(key))
            repo_owner = repo_owner or owner
            repo_name = repo_name or name
            if repo_owner and repo_name:
                break

    activity_type = classify_cwd_activity(cwd)

    if repo_owner and repo_name and branch:
        return RepoAttribution(repo_owner, repo_name, branch, cwd, activity_type)

    if cwd:
        cwd_path = Path(cwd).expanduser()
        # Static remote-host mappings are authoritative for known collector
        # paths. They avoid accidentally attributing remote paths to whatever
        # repo happens to exist at the same path on the current machine.
        known = _known_root_match(str(cwd_path))
        if known:
            owner, name = known
            repo_owner = repo_owner or owner
            repo_name = repo_name or name
        if cwd_path.exists():
            remote = _git(cwd_path, "remote", "get-url", "origin")
            owner, name = parse_repo_url(remote or "")
            repo_owner = repo_owner or owner
            repo_name = repo_name or name
            branch = branch or _git(cwd_path, "symbolic-ref", "--short", "HEAD")

    return RepoAttribution(repo_owner, repo_name, branch, cwd, activity_type)


def enrich_raw_repo_attribution(raw_data: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Copy ``raw_data`` and fill Drover's canonical attribution keys."""
    enriched = dict(raw_data or {})
    attr = derive_repo_attribution(enriched)
    if attr.repo_owner:
        enriched.setdefault("_repo_owner", attr.repo_owner)
    if attr.repo_name:
        enriched.setdefault("_repo_name", attr.repo_name)
    if attr.branch:
        enriched.setdefault("gitBranch", attr.branch)
    if attr.activity_type:
        enriched.setdefault("_nexus_activity_type", attr.activity_type)
    return enriched
