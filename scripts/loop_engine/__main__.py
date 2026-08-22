"""Run one Phase 0 goal loop. Stopped by a human, not by the loop.

    python -m loop_engine --host mac-mini --repo /path/to/checkout

Refuses to run on `main`. §6.6 asks for that and the design is explicit that
the caps are enforced by the driver rather than by prompting -- a rule the
agent is merely told about is a rule that holds until it decides otherwise.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import click

from .api import HarnessApi
from .driver import Caps, LoopDriver
from .goals import goal_a
from .ledger import ScratchLedger

_PROTECTED_BRANCHES = {"main", "master"}


def _current_branch(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


@click.command()
@click.option("--host", "host_id", required=True, help="Host to run iterations on.")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Checkout the goal is about. Must not be on main.",
)
@click.option("--hub", default="http://127.0.0.1:7080", show_default=True)
@click.option("--token", envvar="DROVER_API_TOKEN", required=True)
@click.option("--harness", default="claude", show_default=True)
@click.option("--max-iterations", default=10, show_default=True)
@click.option("--max-spend-usd", default=5.0, show_default=True)
@click.option(
    "--scratch",
    type=click.Path(path_type=Path),
    default=Path.home() / ".drover" / "loop-phase0.duckdb",
    show_default=True,
    help="Scratch ledger. Deliberately not the analytical store.",
)
def main(
    host_id: str,
    repo: Path,
    hub: str,
    token: str,
    harness: str,
    max_iterations: int,
    max_spend_usd: float,
    scratch: Path,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    branch = _current_branch(repo)
    if branch in _PROTECTED_BRANCHES or not branch:
        raise click.ClickException(
            f"refusing to run on {branch or 'an unknown branch'}; "
            "check out a working branch first"
        )

    goal = goal_a(repo)
    driver = LoopDriver(
        api=HarnessApi(base_url=hub, token=token),
        ledger=ScratchLedger(scratch),
        goal=goal,
        host_id=host_id,
        harness=harness,
        caps=Caps(max_iterations=max_iterations, max_spend_usd=max_spend_usd),
    )

    click.echo(f"goal   {goal.goal_id} ({goal.kind})")
    click.echo(f"check  {' '.join(goal.check)}")
    click.echo(f"branch {branch}")
    click.echo(f"caps   {max_iterations} iterations, {max_spend_usd} USD")
    click.echo("")

    outcomes = driver.run()
    met = any(o.met for o in outcomes)
    click.echo("")
    click.echo(f"{len(outcomes)} iteration(s); goal {'met' if met else 'not met'}")
    click.echo(f"ledger {scratch}")
    sys.exit(0)


if __name__ == "__main__":
    main()
