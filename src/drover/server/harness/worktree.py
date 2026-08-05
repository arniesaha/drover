"""Per-session git worktrees for approval-less structured harnesses.

Codex and Gemini structured sessions run headless with no wire-level
approval channel (see ``structured/codex.py`` and ``structured/gemini.py``),
so the daemon gives them full-auto execution -- which is only safe when a
session cannot touch the user's main checkout. Each such session gets its
own worktree on a ``drover/<session-id>`` branch: a broad ``git add -A``
sweeps only that session's files, and its commits sit on the session branch
until the user merges them deliberately.

Every function here is best-effort and never raises: a host where ``cwd``
isn't a git repo (or git itself misbehaves) falls back to running the
session in place, which is exactly the pre-worktree behavior.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("drover.harnessd")

_GIT_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class SessionWorktree:
    repo_root: str
    path: str
    branch: str
    base_sha: str


def _git(cwd: str, *args: str) -> str | None:
    """Run git, returning stripped stdout, or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("git %s failed in %s: %s", args, cwd, exc)
        return None
    if result.returncode != 0:
        log.debug("git %s failed in %s: %s", args, cwd, result.stderr.strip())
        return None
    return result.stdout.strip()


def create_session_worktree(
    cwd: str, session_id: str, worktrees_dir: Path
) -> SessionWorktree | None:
    """Create a worktree for one session, or None to run in place.

    None (not an exception) covers every unsuitable case: ``cwd`` outside a
    git repo, a repo with no commits yet (nothing to base a worktree on), or
    a git failure.
    """
    repo_root = _git(cwd, "rev-parse", "--show-toplevel")
    if repo_root is None:
        return None
    repo_root = str(Path(repo_root).resolve())
    base_sha = _git(repo_root, "rev-parse", "HEAD")
    if base_sha is None:
        return None
    path = worktrees_dir / session_id
    branch = f"drover/{session_id}"
    try:
        worktrees_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.debug("cannot create worktrees dir %s: %s", worktrees_dir, exc)
        return None
    added = _git(repo_root, "worktree", "add", str(path), "-b", branch)
    if added is None:
        return None
    return SessionWorktree(
        repo_root=repo_root,
        path=str(path),
        branch=branch,
        base_sha=base_sha,
    )


def cleanup_session_worktree(wt: SessionWorktree) -> str:
    """Reclaim a session worktree if (and only if) the session left no work.

    Returns ``"removed"`` when the worktree was untouched (clean tree, no
    commits past base) and both it and its branch were deleted;
    ``"kept"`` when there is uncommitted or committed session work to
    preserve; ``"missing"`` when the worktree directory no longer exists
    (its stale registration is pruned so the path can be reused).
    """
    if not Path(wt.path).is_dir():
        _git(wt.repo_root, "worktree", "prune")
        return "missing"
    status = _git(wt.path, "status", "--porcelain")
    if status is None or status != "":
        return "kept"
    head = _git(wt.path, "rev-parse", "HEAD")
    if head != wt.base_sha:
        return "kept"
    if _git(wt.repo_root, "worktree", "remove", wt.path) is None:
        return "kept"
    _git(wt.repo_root, "branch", "-D", wt.branch)
    return "removed"
