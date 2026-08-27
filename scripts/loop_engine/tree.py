"""Git facts about the working tree an iteration ran against.

Decision D1 splits continuity in two: claims live in the ledger and are
rendered into the brief; artifacts live in git and the agent reads them from
the working tree. Nothing joined the halves, so a claim could outlive the
commit behind it and the next brief would still present it as standing. That
happened on the first live run -- the agent noticed the mismatch itself and
had to re-derive work a prior iteration claimed to have finished.

These are the joins: what commit an iteration started from, what it left
behind, and whether that is still in the tree the next one sees.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def head_commit(repo: Path) -> Optional[str]:
    """The full SHA at HEAD, or None if this is not a usable checkout.

    None rather than raising: a driver that cannot read git should still run
    the iteration and record that it could not, the same way an iteration
    whose session failed to launch is still a row.
    """
    result = _git(repo, "rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_reachable(repo: Path, commit: str) -> bool:
    """Whether `commit` is an ancestor of -- or is -- the current HEAD.

    A rebase, a squash-merge or a reset all leave a recorded commit
    unreachable. This does not judge which happened; it reports the fact so
    the brief can say it plainly.
    """
    if not commit:
        return False
    return _git(repo, "merge-base", "--is-ancestor", commit, "HEAD").returncode == 0
