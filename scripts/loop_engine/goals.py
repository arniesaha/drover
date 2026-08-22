"""What the loop is trying to make true, and how it checks.

A goal owns a *runnable* done-condition. Prose does not qualify: the whole
failure mode this phase exists to catch is a loop that claims progress it did
not make, and the only thing that catches it on the same iteration is an exit
code.

Goal A is deliberately small (decision R1). "Clear the annotation debt until
mypy exits clean" was written when the package was 34,286 lines; it is now
56,773, which makes it a project rather than a calibration. The point of the
first goal is that the *instrumentation* is under test, not the work.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Verification:
    """One run of a goal's done-condition."""

    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def met(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class Goal:
    """A standing objective with a check a machine can run.

    ``kind`` distinguishes the two shapes the design calls for: a *project*
    goal completes, an *invariant* goal never does and expresses a level to
    hold. Modelling an invariant as a project guarantees either a false
    completion or an infinite loop, so the loop asks the goal which it is
    rather than inferring it.
    """

    goal_id: str
    kind: str  # "project" | "invariant"
    summary: str
    check: tuple[str, ...]
    cwd: Path

    def verify(self, *, timeout_s: float = 900) -> Verification:
        """Run the done-condition. Never raises; a broken check is a result.

        A check that cannot run is not the same as a goal that is not met, but
        both are things the loop has to record rather than crash on. The exit
        code carries the distinction: 127 is the shell's own "command not
        found", and a timeout reports 124 the way `timeout(1)` does.
        """
        try:
            completed = subprocess.run(
                self.check,
                cwd=str(self.cwd),
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except FileNotFoundError:
            return Verification(tuple(self.check), 127, "", "check command not found")
        except subprocess.TimeoutExpired:
            return Verification(
                tuple(self.check), 124, "", f"check exceeded {timeout_s}s"
            )
        return Verification(
            tuple(self.check),
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )


def goal_a(repo_root: Path) -> Goal:
    """Calibration: one bounded package to mypy-clean.

    Nine errors across 2,283 lines when this was written. Small enough that a
    handful of iterations exercises the ledger, the brief, and the
    done-condition without the work itself becoming the experiment.
    """
    return Goal(
        goal_id="goal-a-providers-mypy",
        kind="project",
        summary=(
            "Make `mypy --ignore-missing-imports src/drover/server/providers/` "
            "exit clean, without weakening the checks or adding blanket ignores."
        ),
        # `sys.executable`, not "python". The first accidental run of this
        # driver recorded ten iterations at exit 127 -- the code this module
        # uses for "check command not found" -- because the `python` on PATH
        # was not the interpreter the driver was running under and had no
        # mypy. The instrumentation caught it, which is the point of Goal A.
        check=(
            sys.executable,
            "-m",
            "mypy",
            "--ignore-missing-imports",
            "src/drover/server/providers/",
        ),
        cwd=repo_root,
    )
