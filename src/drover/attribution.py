"""Best-effort project/repository attribution for agent events.

Collectors run on the same host as the source harness, so this is the right
place to turn a raw ``cwd`` into stable repo metadata before the event is
shipped to the Mac Mini lakehouse.
"""

from __future__ import annotations

import html
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from drover.task_id import parse_repo_url

# ---------------------------------------------------------------------------
# Static known-roots mapping
# ---------------------------------------------------------------------------
# Events collected on remote hosts (e.g. the NAS running OpenClaw) carry a
# ``cwd`` that is valid on *that* host but doesn't exist on the Mac Mini
# lakehouse.  The ``cwd_path.exists()`` guard below silently skips those
# paths, leaving attribution at 0%.  This mapping lets us resolve the most
# common NAS project paths deterministically without a live ``git`` call.
#
# Keys are path *prefixes* (no trailing slash).  The longest matching prefix
# wins, so per-project subdirectories can be added later without ambiguity.
_KNOWN_ROOTS: dict[str, tuple[str, str]] = {
    # Project-scoped clawd workspaces. These intentionally precede the broader
    # /home/Arnab/clawd runtime root via longest-prefix matching.
    "/home/Arnab/clawd/projects/healthos": ("arniesaha", "healthos"),
    "/home/Arnab/clawd/projects/ai-ops-studio": ("arniesaha", "ai-ops-studio"),
    "/home/Arnab/dev/nexus": ("arniesaha", "nexus"),
    "/home/Arnab/dev/agentweave": ("arniesaha", "agentweave"),
    "/home/Arnab/dev/openclaw": ("arniesaha", "openclaw"),
    # OpenClaw runtime workspace observed from the NAS collector.  Keep this
    # exact root explicit so /home/Arnab itself is not broadly attributed.
    "/home/Arnab/clawd": ("arniesaha", "openclaw"),
    "/home/Arnab/dev/portfolio": ("arniesaha", "portfolio"),
    "/home/Arnab/dev/mux": ("arniesaha", "mux"),
    "/home/Arnab/dev/agent-max": ("arniesaha", "agent-max"),
    "/home/Arnab/dev/agent-shared": ("arniesaha", "agent-shared"),
    # Paperclip pod workspaces are remote/container paths. They do not exist on
    # the Mac Mini lakehouse host, but the terminal repo segment is stable.
    "/paperclip/home/instances/default/workspaces/e46aa686-4fa6-414c-94a7-946538fb308f/nexus": (
        "arniesaha",
        "nexus",
    ),
    "/paperclip/home/instances/default/workspaces/e46aa686/4fa6/414c/94a7/946538fb308f/nexus": (
        "arniesaha",
        "nexus",
    ),
    # Mac Mini Jenny project roots observed locally. Do not map /Users/arnabmac
    # or Claude/Hermes memory folders; those remain visible in runtime-audit as
    # intentionally unattributed unless events include a project cwd.
    "/Users/arnabmac/.hermes/hermes-agent": ("NousResearch", "hermes-agent"),
    "/Users/arnabmac/jenny/nexus": ("arniesaha", "nexus"),
    "/Users/arnabmac/jenny/agent-foundry": ("arniesaha", "agent-foundry"),
    "/Users/arnabmac/jenny/agent-shared": ("arniesaha", "agent-shared"),
}
GENERAL_WORKSPACE_ACTIVITY_TYPE = "general_workspace"
GENERAL_WORKSPACE_ROOTS = frozenset(
    {
        # NAS home-directory sessions are often shell/config/memory traffic, not
        # a project checkout. Keep this exact so real project subdirectories
        # still need explicit safe mappings or git metadata.
        "/home/Arnab",
        # Mac Mini home-directory/Claude memory observer sessions are likewise
        # general context, not a safe signal that all activity belongs to a repo.
        "/Users/arnabmac",
        "/Users/arnabmac/.claude-mem/observer-sessions",
    }
)

_OWNER_REPO_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
_WORKING_DIRECTORY_XML_RE = re.compile(
    r"<working_directory>\s*([^<]+?)\s*</working_directory>", re.IGNORECASE
)


def _known_root_match(cwd: str) -> Optional[tuple[str, str]]:
    """Return known-root attribution for exact roots or path descendants."""
    for prefix, attribution in sorted(
        _KNOWN_ROOTS.items(), key=lambda kv: len(kv[0]), reverse=True
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
    if normalized in GENERAL_WORKSPACE_ROOTS:
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
    git when ``cwd`` exists on this host. If historical Mac Mini ingest sees a
    NAS-only path, the event remains unattributed rather than guessing.
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
        # paths. They avoid accidentally attributing NAS paths to whatever repo
        # happens to exist at the same path on the machine running tests/audits.
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
