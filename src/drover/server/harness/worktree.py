"""Per-session git worktrees for approval-less structured harnesses.

Codex and Agy structured sessions run headless with no wire-level
approval channel (see ``structured/codex.py`` and ``structured/agy.py``),
so the daemon gives them full-auto execution -- which is only safe when a
session cannot touch the user's main checkout. Each such session gets its
own worktree on a ``drover/<session-id>`` branch: a broad ``git add -A``
sweeps only that session's files, and its commits sit on the session branch
until the user merges them deliberately.

A host where ``cwd`` isn't a git repo, or has no commits yet, falls back to
running the session in place: that is the pre-worktree behavior and there is
no isolation to lose.

A worktree that *fails* is different, and raises ``WorktreeIsolationUnavailable``
rather than falling back. Conflating the two is how a transient git stall came
to silently strip isolation from a full-auto session: on the reference hub
``git worktree add`` took 103 seconds against 94 accumulated worktrees, blew the
15 second timeout, and two codex sessions ran with ``danger-full-access`` on
``main`` in the user's shared checkout. Running in place is a correct answer to
"no worktree is possible" and a dangerous one to "the worktree broke".
"""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("drover.harnessd")

_GIT_TIMEOUT_SECONDS = 15
# Creating the worktree gets a launch-sized budget rather than a probe's.
# Process start on the reference hub's external SSD spikes past 11s
# (drover#321), and with 15s this call timed out twice in two minutes, so the
# fail-closed path correctly refused codex launches that would have worked.
# It must stay under the hub's 120s CREATE_SESSION_TIMEOUT_S.
_WORKTREE_ADD_TIMEOUT_SECONDS = 90


class WorktreeIsolationUnavailable(RuntimeError):
    """Isolation was required, attempted, and could not be established.

    Raised only for a genuine failure (git timed out, git errored, the
    worktrees directory could not be created). Never raised for a directory
    that simply cannot host a worktree, which returns None instead.
    """


def claim_worktrees_directory(worktrees_dir: Path) -> int:
    """Hold exclusive ownership until the daemon closes the returned fd.

    A second daemon must not sweep clean worktrees belonging to live sessions
    in the first daemon. The lock file is never unlinked: a new inode would
    let another daemon acquire an independent lock for the same directory.
    """
    try:
        worktrees_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            worktrees_dir / ".drover-owner.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
    except OSError as exc:
        raise WorktreeIsolationUnavailable(
            f"cannot claim worktrees directory {worktrees_dir}: {exc}"
        ) from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise WorktreeIsolationUnavailable(
            f"worktrees directory is already in use: {worktrees_dir}"
        ) from exc
    return fd


@dataclass(frozen=True)
class SessionWorktree:
    repo_root: str
    path: str
    branch: str
    base_sha: str


def _git(
    cwd: str, *args: str, required: bool = False, timeout: float | None = None
) -> str | None:
    """Run git, returning stripped stdout, or None on any failure.

    ``required`` marks a call whose failure means isolation could not be
    established, as opposed to a probe whose negative answer is information.
    A required call raises instead of returning None, so the caller cannot
    accidentally treat "it broke" as "it does not apply".
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=timeout if timeout is not None else _GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("git %s failed in %s: %s", args, cwd, exc)
        if required:
            raise WorktreeIsolationUnavailable(
                f"git {' '.join(args)} failed in {cwd}: {exc}"
            ) from exc
        return None
    if result.returncode != 0:
        log.debug("git %s failed in %s: %s", args, cwd, result.stderr.strip())
        if required:
            raise WorktreeIsolationUnavailable(
                f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
            )
        return None
    return result.stdout.strip()


def create_session_worktree(
    cwd: str, session_id: str, worktrees_dir: Path
) -> SessionWorktree | None:
    """Create a worktree for one session, or None to run in place.

    None covers the unsuitable cases, where there is no isolation to lose:
    ``cwd`` outside a git repo, or a repo with no commits yet. A git failure
    raises ``WorktreeIsolationUnavailable`` instead, because falling back
    would hand a full-auto session the user's own checkout.
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
        raise WorktreeIsolationUnavailable(
            f"cannot create worktrees dir {worktrees_dir}: {exc}"
        ) from exc
    # A repeated session id can name a real, dirty worktree. Reserve the
    # branch separately: `git branch` fails atomically if another session
    # already owns it, and a later add failure then cannot claim that branch.
    if os.path.lexists(path):
        raise WorktreeIsolationUnavailable(
            f"session worktree path already exists: {path}"
        )
    _git(repo_root, "branch", branch, base_sha, required=True)
    try:
        _git(
            repo_root,
            "worktree",
            "add",
            str(path),
            branch,
            required=True,
            timeout=_WORKTREE_ADD_TIMEOUT_SECONDS,
        )
    except WorktreeIsolationUnavailable:
        # Never remove the path here: a concurrent creator could have put
        # files in it after our absence check, including ignored-only files.
        # The branch was reserved by this invocation. If no path exists and it
        # still points to our base, Git may safely delete it; otherwise leave
        # the partial worktree for the later, conservative startup sweep.
        _git(repo_root, "worktree", "prune")
        if (
            not os.path.lexists(path)
            and _git(repo_root, "rev-parse", branch) == base_sha
        ):
            _git(repo_root, "branch", "-d", branch)
        raise
    return SessionWorktree(
        repo_root=repo_root,
        path=str(path),
        branch=branch,
        base_sha=base_sha,
    )


def reclaim_stale_session_worktrees(
    worktrees_dir: Path, *, candidates: Iterable[Path] | None = None
) -> dict[str, int]:
    """Reclaim session worktrees left behind by a previous daemon run.

    The daemon tracks live session worktrees in an in-memory map that a restart
    drops, so a clean worktree created before a crash/restart would otherwise
    sit on disk forever (#398). Each directory under ``worktrees_dir`` is
    reconstructed into a ``SessionWorktree`` and put through the same
    keep-if-there-is-work policy as session-end cleanup.

    Returns counts keyed by the cleanup outcome (``removed``/``kept``/
    ``missing``/``orphaned``), plus ``skipped`` for entries that are not a
    drover session worktree.
    """
    counts: dict[str, int] = {
        "removed": 0,
        "kept": 0,
        "missing": 0,
        "orphaned": 0,
        "skipped": 0,
    }
    worktrees_dir = Path(worktrees_dir)
    if not worktrees_dir.is_dir():
        return counts
    for entry in sorted(
        candidates if candidates is not None else worktrees_dir.iterdir()
    ):
        if not entry.is_dir():
            continue
        wt = _reconstruct_worktree(entry)
        if wt is None:
            counts["skipped"] += 1
            continue
        counts[cleanup_session_worktree(wt)] += 1
    return counts


def _reconstruct_worktree(path: Path) -> SessionWorktree | None:
    """Rebuild a ``SessionWorktree`` from its directory alone.

    Returns None when the directory is not a drover session worktree (no git
    metadata, a detached HEAD, or a non-``drover/`` branch), which the sweep
    skips rather than guessing at. ``repo_root`` is the shared checkout's top
    level, recovered through the common git dir so ``worktree remove`` and
    ``update-ref`` run from a worktree git will not refuse to act on.
    """
    common_dir = _git(
        str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if common_dir is None:
        return None
    repo_root = str(Path(common_dir).resolve().parent)
    branch = _git(str(path), "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None or branch == "HEAD" or not branch.startswith("drover/"):
        return None
    # The fork point is where the session branch left the main branch, which is
    # what create_session_worktree captured as base_sha. merge-base stays put as
    # the main branch advances after the worktree was created.
    base_sha = _git(repo_root, "merge-base", branch, "HEAD")
    if base_sha is None:
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
    (its stale registration is pruned so the path can be reused);
    ``"orphaned"`` when the worktree was removed but its branch changed or
    could not be deleted safely.
    """
    if not Path(wt.path).is_dir():
        _git(wt.repo_root, "worktree", "prune")
        return "missing"
    # Git's worktree removal deletes ignored files too. They can contain the
    # only copy of session output, so a normal porcelain status is not enough
    # evidence that this directory is disposable.
    status = _git(
        wt.path, "status", "--porcelain", "--ignored", "--untracked-files=all"
    )
    if status is None or status != "":
        return "kept"
    head = _git(wt.path, "rev-parse", "HEAD")
    if head != wt.base_sha:
        return "kept"
    if _git(wt.repo_root, "worktree", "remove", wt.path) is None:
        return "kept"
    # Compare-and-delete the exact original ref. `branch -D` could discard a
    # concurrent commit; `branch -d` depends on the primary checkout's HEAD.
    if (
        _git(
            wt.repo_root,
            "update-ref",
            "-d",
            f"refs/heads/{wt.branch}",
            wt.base_sha,
        )
        is None
    ):
        return "orphaned"
    return "removed"
