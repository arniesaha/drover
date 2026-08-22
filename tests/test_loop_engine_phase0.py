"""Cover the offline half of the Phase 0 driver.

The loop itself needs a harness to talk to; these are the parts that decide
what gets recorded and what "done" means, and they are worth holding still
independently of whether a session ever starts.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loop_engine.brief import render
from loop_engine.goals import Goal, goal_a
from loop_engine.ledger import Iteration, ScratchLedger

_NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def _goal(check, tmp_path: Path, kind: str = "project") -> Goal:
    return Goal(
        goal_id="g1", kind=kind, summary="make it so", check=check, cwd=tmp_path
    )


def test_a_goal_is_met_only_when_its_check_exits_zero(tmp_path: Path) -> None:
    met = _goal((sys.executable, "-c", "raise SystemExit(0)"), tmp_path).verify()
    unmet = _goal((sys.executable, "-c", "raise SystemExit(3)"), tmp_path).verify()

    assert met.met is True and met.exit_code == 0
    assert unmet.met is False and unmet.exit_code == 3


def test_a_check_that_cannot_run_is_a_result_not_a_crash(tmp_path: Path) -> None:
    """The loop has to record a broken check, not die of it.

    127 is the shell's own "command not found", so the distinction between
    "the goal is not met" and "the check never ran" survives into the ledger.
    """
    outcome = _goal(("/nonexistent/checker",), tmp_path).verify()

    assert outcome.exit_code == 127
    assert outcome.met is False


def test_a_check_that_hangs_is_bounded(tmp_path: Path) -> None:
    outcome = _goal(
        (sys.executable, "-c", "import time; time.sleep(30)"), tmp_path
    ).verify(timeout_s=0.5)

    assert outcome.exit_code == 124
    assert "0.5" in outcome.stderr


def test_goal_a_names_a_bounded_target(tmp_path: Path) -> None:
    """R1: the calibration goal is one package, not the whole annotation debt."""
    goal = goal_a(tmp_path)

    assert goal.kind == "project"
    assert "src/drover/server/providers/" in goal.check
    assert "mypy" in goal.check


def test_the_ledger_records_the_two_columns_the_profile_question_needs(
    tmp_path: Path,
) -> None:
    """R4: `harness` and `brief_rendered`, verbatim.

    Without them the brief corpus cannot say whether a brief that worked was
    harness-specific, and a portable profile would have to be specified by
    guesswork rather than from evidence.
    """
    ledger = ScratchLedger(tmp_path / "scratch.duckdb")
    ledger.append(
        Iteration(
            goal_id="g1",
            ordinal=1,
            started_at=_NOW,
            ended_at=_NOW + timedelta(minutes=4),
            harness="claude",
            brief_rendered="# Goal: make it so\n\nfirst turn",
            verification_exit=1,
            cost_usd=0.42,
        )
    )

    (row,) = ledger.history("g1")
    assert row["harness"] == "claude"
    assert row["brief_rendered"].startswith("# Goal: make it so")
    assert row["verification_exit"] == 1


def test_the_ledger_carries_the_caps_the_driver_enforces(tmp_path: Path) -> None:
    """Spend and ordinal come from the record, not from the loop's memory.

    A cap the driver holds in a variable is a cap that resets when the driver
    restarts, and the design is explicit that both caps are enforced by the
    driver rather than by prompting.
    """
    ledger = ScratchLedger(tmp_path / "scratch.duckdb")
    for ordinal, cost in ((1, 0.10), (2, 0.25)):
        ledger.append(
            Iteration(goal_id="g1", ordinal=ordinal, started_at=_NOW, cost_usd=cost)
        )

    assert ledger.next_ordinal("g1") == 3
    assert ledger.next_ordinal("other-goal") == 1
    assert round(ledger.spend_usd("g1"), 2) == 0.35


def test_the_brief_carries_the_check_and_what_it_last_said() -> None:
    brief = render(
        goal_summary="make it so",
        goal_kind="project",
        check=("python", "-m", "mypy", "src/"),
        history=[
            {
                "ordinal": 1,
                "verification_exit": 1,
                "claimed_outcome": "annotated two files",
                "verification_tail": "error: Missing return type",
            }
        ],
        max_iterations=5,
    )

    assert "python -m mypy src/" in brief
    assert "iteration 2 of at most 5" in brief
    assert "Iteration 1: check failed (exit 1)" in brief
    assert "annotated two files" in brief
    assert "error: Missing return type" in brief


def test_the_brief_tells_an_invariant_goal_not_to_finish() -> None:
    """Modelling an invariant as a project guarantees a false completion."""
    brief = render(
        goal_summary="keep it bug-free",
        goal_kind="invariant",
        check=("true",),
        history=[],
        max_iterations=50,
    )

    assert "does not complete" in brief


class _FakeApi:
    """A harness that answers immediately and remembers what it was asked."""

    def __init__(self, *, fail_create: bool = False) -> None:
        self.fail_create = fail_create
        self.prompts: list[str] = []
        self.terminated: list[str] = []
        self._n = 0

    def create_session(self, host_id, *, harness, cwd, command=None, prompt=None):
        if self.fail_create:
            from loop_engine.api import HarnessApiError

            raise HarnessApiError("no such host")
        self._n += 1
        self.prompts.append(prompt or "")
        return {"session_id": f"sess-{self._n}"}

    def session(self, session_id):
        return {"status": "running", "awaiting": "input"}

    def messages(self, session_id):
        return [{"role": "assistant", "text": f"did some work in {session_id}"}]

    def terminate(self, session_id):
        self.terminated.append(session_id)


def _driver(tmp_path: Path, api, *, check, kind="project", caps=None):
    from loop_engine.driver import Caps, LoopDriver

    return LoopDriver(
        api=api,
        ledger=ScratchLedger(tmp_path / "scratch.duckdb"),
        goal=_goal(check, tmp_path, kind=kind),
        host_id="test-host",
        harness="claude",
        caps=caps or Caps(max_iterations=3, max_spend_usd=5.0),
        sleep=lambda _s: None,
    )


def test_a_project_goal_stops_when_its_check_passes(tmp_path: Path) -> None:
    api = _FakeApi()
    outcomes = _driver(
        tmp_path, api, check=(sys.executable, "-c", "raise SystemExit(0)")
    ).run()

    assert [o.ordinal for o in outcomes] == [1]
    assert outcomes[0].met is True
    assert api.terminated == ["sess-1"]


def test_an_invariant_goal_does_not_stop_on_a_passing_check(tmp_path: Path) -> None:
    """An invariant expresses a level to hold, so a clean pass is not an exit.

    Practice also suggests not terminating on a clean pass even for discovery:
    the sessions that validate prior fixes are the ones that surface new
    defects.
    """
    api = _FakeApi()
    outcomes = _driver(
        tmp_path,
        api,
        check=(sys.executable, "-c", "raise SystemExit(0)"),
        kind="invariant",
    ).run()

    assert [o.ordinal for o in outcomes] == [1, 2, 3]
    assert all(o.met for o in outcomes)


def test_the_iteration_cap_is_enforced_by_the_driver(tmp_path: Path) -> None:
    api = _FakeApi()
    outcomes = _driver(
        tmp_path, api, check=(sys.executable, "-c", "raise SystemExit(1)")
    ).run()

    assert [o.ordinal for o in outcomes] == [1, 2, 3]
    assert api.terminated == ["sess-1", "sess-2", "sess-3"]


def test_the_spend_cap_is_read_from_the_ledger_not_from_memory(tmp_path: Path) -> None:
    """A cap held in a variable resets when the driver restarts."""
    from loop_engine.driver import Caps

    ledger = ScratchLedger(tmp_path / "scratch.duckdb")
    ledger.append(Iteration(goal_id="g1", ordinal=1, started_at=_NOW, cost_usd=9.99))

    driver = _driver(
        tmp_path,
        _FakeApi(),
        check=(sys.executable, "-c", "raise SystemExit(1)"),
        caps=Caps(max_iterations=10, max_spend_usd=5.0),
    )
    outcomes = driver.run()

    assert outcomes == []


def test_a_harness_that_will_not_start_still_records_an_iteration(
    tmp_path: Path,
) -> None:
    """A run whose sessions all failed must not look like a run that found nothing."""
    api = _FakeApi(fail_create=True)
    driver = _driver(tmp_path, api, check=(sys.executable, "-c", "raise SystemExit(1)"))
    outcomes = driver.run()

    assert len(outcomes) == 3
    rows = ScratchLedger(tmp_path / "scratch.duckdb").history("g1")
    assert len(rows) == 3
    assert all("harness error" in (r["note"] or "") for r in rows)
    assert api.terminated == []


def test_the_second_brief_carries_the_first_iteration_forward(tmp_path: Path) -> None:
    """Continuity across a context boundary is the whole continuation property."""
    api = _FakeApi()
    _driver(tmp_path, api, check=(sys.executable, "-c", "raise SystemExit(1)")).run()

    assert "None. This is the first." in api.prompts[0]
    assert "Iteration 1: check failed (exit 1)" in api.prompts[1]
    assert "did some work in sess-1" in api.prompts[1]


def test_a_permanent_harness_failure_stops_the_loop_instead_of_repeating(
    tmp_path: Path,
) -> None:
    """A 401 will fail identically next iteration; ten rows say nothing new.

    The first real run of this driver spent its whole ten-iteration budget on
    one bad token and wrote ten identical rows. Retrying a request the server
    has already judged malformed is not patience.
    """
    from loop_engine.api import HarnessApiError

    class _Unauthorized(_FakeApi):
        def create_session(self, host_id, *, harness, cwd, command=None, prompt=None):
            raise HarnessApiError("create_session returned 401", fatal=True)

    api = _Unauthorized()
    outcomes = _driver(
        tmp_path, api, check=(sys.executable, "-c", "raise SystemExit(1)")
    ).run()

    assert len(outcomes) == 1
    assert outcomes[0].fatal is True
    assert len(ScratchLedger(tmp_path / "scratch.duckdb").history("g1")) == 1


def test_goal_a_checks_with_the_interpreter_the_driver_is_running_under(
    tmp_path: Path,
) -> None:
    """ "python" on PATH is not necessarily the venv that has mypy.

    That mistake produced exit 127 on every iteration of the first run, which
    this module's own code documents as "check command not found".
    """
    assert goal_a(tmp_path).check[0] == sys.executable
