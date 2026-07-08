"""Task-ID derivation per spec §4.1.

task_id = sha256(coalesce($DROVER_TASK_ID, repo_owner/repo_name@branch))[:16]

Used everywhere a row is written so multi-session work on the same
(repo, branch) joins back together regardless of which agent ran it.
"""

import hashlib
import re
from typing import Optional, Tuple

_REPO_URL_RE = re.compile(
    r"(?:git@|https?://)([^:/]+)[:/]([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def compute_task_id(
    env_task_id: Optional[str],
    repo_owner: Optional[str],
    repo_name: Optional[str],
    branch: Optional[str],
) -> str:
    """Return a 16-char hex task ID."""
    if env_task_id:
        raw = env_task_id
    else:
        owner = repo_owner or "unknown"
        name = repo_name or "unknown"
        br = branch or "HEAD"
        raw = f"{owner}/{name}@{br}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def parse_repo_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse a git remote URL into (owner, repo_name).

    Handles both SSH (git@host:owner/repo.git) and HTTPS forms.
    Returns (None, None) on anything unparseable.
    """
    if not url:
        return None, None
    m = _REPO_URL_RE.match(url.strip())
    if not m:
        return None, None
    _host, owner, name = m.groups()
    return owner, name
