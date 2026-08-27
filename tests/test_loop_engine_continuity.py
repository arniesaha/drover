"""The brief must not present a claim whose artifact is gone as standing.

Decision D1 splits continuity: claims render into the brief, artifacts live in
git. On the first live run the halves came apart -- the ledger said iteration 2
passed, its commit was not in the tree, and the brief repeated the claim
anyway. The agent caught it and re-derived the work, which is luck, not a
property.
"""

from __future__ import annotations

from loop_engine.brief import render


def _render(history):
    return render(
        goal_summary="make it type-check",
        goal_kind="project",
        check=("mypy", "src/"),
        history=history,
        max_iterations=10,
    )


def _row(ordinal, **kwargs):
    row = {
        "ordinal": ordinal,
        "verification_exit": 0,
        "claimed_outcome": "Done, committed as abc1234.",
        "verification_tail": "Success",
        "base_ref": "a" * 40,
        "diff_ref": "b" * 40,
    }
    row.update(kwargs)
    return row


def test_a_claim_whose_commit_is_gone_is_marked_as_gone():
    text = _render([_row(1, artifact_present=False)])
    assert "not in this tree" in text
    assert "bbbbbbb" in text, "name the commit, so it can be looked for"
    # The claim is still shown -- the agent decides what to do about it.
    assert "Done, committed as abc1234." in text


def test_a_claim_whose_commit_survives_is_not_annotated():
    text = _render([_row(1, artifact_present=True)])
    assert "not in this tree" not in text
    assert "committed nothing" not in text


def test_an_iteration_that_committed_nothing_says_so():
    """base == head means the session left no artifact behind."""
    text = _render([_row(1, diff_ref="a" * 40, artifact_present=True)])
    assert "committed nothing" in text


def test_history_without_refs_renders_as_before():
    """Rows written before this existed carry no refs and must still render."""
    text = _render(
        [{"ordinal": 1, "verification_exit": 1, "claimed_outcome": "Tried."}]
    )
    assert "Iteration 1" in text
    assert "not in this tree" not in text
    assert "committed nothing" not in text


def test_the_ledger_round_trips_the_pair_of_refs(tmp_path):
    from loop_engine.ledger import Iteration, ScratchLedger, utcnow

    ledger = ScratchLedger(tmp_path / "l.duckdb")
    ledger.append(
        Iteration(
            goal_id="g",
            ordinal=1,
            started_at=utcnow(),
            base_ref="a" * 40,
            diff_ref="b" * 40,
        )
    )
    row = ledger.history("g")[0]
    assert row["base_ref"] == "a" * 40
    assert row["diff_ref"] == "b" * 40


def test_a_ledger_written_before_base_ref_gains_the_column(tmp_path):
    """The live ledger has rows that predate this; opening it must not fail."""
    import duckdb
    from loop_engine.ledger import ScratchLedger

    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE goal_iterations ("
        "goal_id VARCHAR NOT NULL, ordinal INTEGER NOT NULL, "
        "started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ, "
        "host_id VARCHAR, session_id VARCHAR, harness VARCHAR, "
        "brief_rendered VARCHAR, verification_cmd VARCHAR, "
        "verification_exit INTEGER, verification_tail VARCHAR, "
        "claimed_outcome VARCHAR, diff_ref VARCHAR, prompt_tokens BIGINT, "
        "completion_tokens BIGINT, cost_usd DOUBLE, note VARCHAR, "
        "PRIMARY KEY (goal_id, ordinal))"
    )
    con.execute(
        "INSERT INTO goal_iterations (goal_id, ordinal, started_at) "
        "VALUES ('g', 1, now())"
    )
    con.close()

    row = ScratchLedger(path).history("g")[0]
    assert row["base_ref"] is None


def test_the_driver_records_where_an_iteration_started_and_ended(tmp_path):
    """base_ref and diff_ref are what let a later brief check the claim."""
    import subprocess
    import sys

    from loop_engine.driver import Caps, LoopDriver
    from loop_engine.goals import Goal
    from loop_engine.ledger import ScratchLedger

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "loop@example.invalid"),
        ("config", "user.name", "Loop"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "first"], check=True)

    class _Api:
        def create_session(self, host_id, *, harness, cwd, command=None, prompt=None):
            # Stand in for an agent that commits its work, as the brief asks.
            (repo / "b.txt").write_text("two\n")
            subprocess.run(["git", "-C", str(repo), "add", "b.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-q", "-m", "agent work"],
                check=True,
            )
            return {"session_id": "s1"}

        def session(self, session_id):
            # Flat, not the envelope: HarnessApi.session unwraps it. A fake
            # returning the envelope is what hid the real bug once already.
            return {"status": "running", "awaiting": "input"}

        def messages(self, session_id):
            return []

        def terminate(self, session_id):
            return None

    ledger = ScratchLedger(tmp_path / "scratch.duckdb")
    LoopDriver(
        api=_Api(),
        ledger=ledger,
        goal=Goal(
            goal_id="g1",
            kind="project",
            summary="s",
            check=(sys.executable, "-c", "raise SystemExit(0)"),
            cwd=repo,
        ),
        host_id="h",
        harness="claude",
        caps=Caps(max_iterations=1, max_tokens=1_000),
        sleep=lambda _s: None,
    ).run_iteration(1)

    row = ledger.history("g1")[0]
    assert row["base_ref"] and row["diff_ref"]
    assert row["base_ref"] != row["diff_ref"], "the agent committed; refs must differ"
