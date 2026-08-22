"""Render the goal brief that carries state across a context boundary.

Fresh session per iteration is the continuation property, not a compromise: a
clean context each turn is what stops long runs degrading. Continuity comes
from two places and the split is deliberate (decision D1) -- artifacts live in
git and the agent reads them from the working tree; claims live here, rendered
into the brief.

How much belongs in each is an open question the design names and does not
settle. That is why the rendered text is stored verbatim on every iteration:
after a run there is a corpus to answer it from rather than an opinion.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

_MAX_TAIL_CHARS = 2000


def render(
    *,
    goal_summary: str,
    goal_kind: str,
    check: Sequence[str],
    history: Sequence[Mapping[str, Any]],
    max_iterations: int,
) -> str:
    """Build the opening turn for one iteration.

    Says what is true, what was tried, and what the check reported -- and
    nothing about how to fix it. The loop is not a prompt-engineering
    experiment; if the agent needs to be told how, that is a finding about the
    goal, not a reason to write a better brief.
    """
    ordinal = len(history) + 1
    lines = [
        f"# Goal: {goal_summary}",
        "",
        f"This is iteration {ordinal} of at most {max_iterations}.",
        "",
        "## How this is checked",
        "",
        f"    {' '.join(check)}",
        "",
        "The goal is met when that command exits 0. Nothing else counts as done,"
        " and saying it is done does not make it so: the same command runs after"
        " your turn and its exit code is what gets recorded.",
        "",
    ]

    if goal_kind == "invariant":
        lines += [
            "This is an invariant goal. It does not complete; it expresses a"
            " level to hold. Do not try to finish it.",
            "",
        ]

    if not history:
        lines += [
            "## Prior iterations",
            "",
            "None. This is the first.",
            "",
        ]
    else:
        lines += ["## Prior iterations", ""]
        for row in history:
            exit_code = row.get("verification_exit")
            verdict = (
                "check passed"
                if exit_code == 0
                else (
                    f"check failed (exit {exit_code})"
                    if exit_code is not None
                    else "check did not run"
                )
            )
            claimed = (row.get("claimed_outcome") or "").strip()
            lines.append(f"- Iteration {row.get('ordinal')}: {verdict}.")
            if claimed:
                lines.append(f"  It reported: {claimed}")
        lines.append("")
        tail = (history[-1].get("verification_tail") or "").strip()
        if tail:
            lines += [
                "## What the check last said",
                "",
                "```",
                tail[-_MAX_TAIL_CHARS:],
                "```",
                "",
            ]

    lines += [
        "## Working agreement",
        "",
        "- The repository is already checked out at your working directory.",
        "- Commit your work. Do not push, and do not work on `main`.",
        "- If you conclude the goal cannot be met as stated, say so plainly and"
        " say why. That is a useful iteration, not a failed one.",
    ]
    return "\n".join(lines)
