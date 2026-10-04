"""Read-only file evidence from a harness's local session worktree.

Capture HEAD before the turn: a clean worktree at its end can still contain
several new commits. Persist the paths on the host, where Git is available,
so memory consumers never have to inspect a remote or deleted worktree.
"""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("drover.harnessd")


def _git(cwd: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("file evidence probe failed in %s: %s", cwd, exc)
        return None
    if result.returncode:
        return None
    # Avoid universal-newline conversion: paths may contain CR and LF.
    # Callers split Git's NUL boundaries rather than parsing display quoting.
    return result.stdout.decode("utf-8", errors="replace")


def capture_head(cwd: str | None) -> str | None:
    if not cwd:
        return None
    head = _git(cwd, "rev-parse", "--verify", "HEAD")
    return head.strip() if head else None


def changed_paths(cwd: str, base: str) -> list[str]:
    """Union committed and outstanding changes since a turn's starting HEAD.

    Commit-by-commit paths retain edits later reverted in the same turn.
    First-parent traversal avoids crediting every commit of a merged branch;
    the merge's own diff still supplies the paths it brought in. Disable rename
    detection so both the removed and added paths survive. The daemon gives
    Codex a dedicated session worktree; outstanding edits belong to that session.
    """
    outputs = [
        _git(
            cwd,
            "log",
            "--first-parent",
            "-m",
            "--format=",
            "--name-only",
            "--no-renames",
            "--no-relative",
            "-z",
            f"{base}..HEAD",
            "--",
        ),
        _git(
            cwd,
            "diff",
            "--name-only",
            "--no-renames",
            "--no-relative",
            "-z",
            base,
            "--",
        ),
        _git(
            cwd,
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "--no-relative",
            "-z",
            base,
            "--",
        ),
        _git(
            cwd,
            "ls-files",
            "--others",
            "--exclude-standard",
            "--full-name",
            "-z",
            "--",
            ":/",
        ),
    ]
    return sorted(
        {path for output in outputs if output for path in output.split("\0") if path}
    )
