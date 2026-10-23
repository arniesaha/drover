from drover.server.harness.worker_state import (
    STATE_CONTRACT,
    authority_block,
    effective_worker_state,
    parse_state_line,
    worker_brief,
)

SHA = "a" * 40


def test_parse_last_valid_state_line_and_fields():
    text = (
        "work in progress\n"
        "Drover-State: working\n"
        f"Drover-State: done sha={SHA} pr=#787\n"
    )
    assert parse_state_line(text) == {"state": "done", "sha": SHA, "pr": 787}


def test_parse_rejects_ambiguous_or_incomplete_declarations():
    assert parse_state_line("Drover-State: done sha=abc1234") is None
    assert parse_state_line("Drover-State: blocked") is None
    assert (
        parse_state_line("Drover-State: needs-approval gate=unknown reason=no") is None
    )


def test_parse_blocked_and_approval_details_are_bounded():
    assert parse_state_line("Drover-State: blocked reason=tests failed") == {
        "state": "blocked",
        "reason": "tests failed",
    }
    assert parse_state_line(
        "Drover-State: needs-approval gate=rollout reason=requires operator secret"
    ) == {
        "state": "needs_approval",
        "gate": "rollout",
        "reason": "requires operator secret",
    }


def test_effective_state_makes_undeclared_idle_a_stall_candidate():
    assert effective_worker_state(status="running", awaiting="input", row=None) == (
        "idle_undeclared"
    )
    assert (
        effective_worker_state(
            status="running",
            awaiting="input",
            row={"state": "done", "declared": True},
        )
        == "done"
    )
    assert (
        effective_worker_state(
            status="running",
            awaiting="input",
            row={"state": "working", "declared": True},
        )
        == "idle_undeclared"
    )
    assert (
        effective_worker_state(status="completed", awaiting=None, row=None) == "ended"
    )


def test_authority_block_uses_live_label_and_hard_stops():
    block = authority_block(
        ["status:queued", "autonomy:through-merge", "autonomy:through-merge"],
        "Acceptance\n- Hard stop: gateway restart\nHard stop: public post",
    )
    assert "approval labels: autonomy:through-merge" in block
    assert "hard stops: gateway restart; public post" in block
    assert "always human: money, public action, deletion, gateway restart" in block


def test_worker_brief_wraps_authority_and_state_contract():
    brief = worker_brief("Do the bounded task.", [], None)
    assert brief.startswith("Authority:\n")
    assert "approval labels: none" in brief
    assert "Do the bounded task." in brief
    assert brief.endswith(STATE_CONTRACT)
