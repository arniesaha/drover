"""P2 (#1024): one verification identity derivation behind four callers.

These drive the real ``WorkService`` against the control Postgres store and
compare what it stores and decides with an independent, written-out digest.
Nothing here inspects source text. A seeded wrong derivation must change an
observable behaviour, not merely a count.
"""

import hashlib
import json

import pytest
from test_work_queue_execution import (  # noqa: F401
    ACTOR,
    complete,
    launch,
    service,
    update,
)
from test_work_queue_github_acceptance import SHA, ready, verdict
from test_work_queue_local_candidates import _deferred_completion_service, _ref

import drover.server.work_queue.service as service_module
from drover.server.work_queue.config import GoalScope
from drover.server.work_queue.service import WorkActor

BODY = "Public behavioral acceptance"
LOCAL_BODY = "Perform only a harmless local canary effect."


def digest(*parts):
    """The pinned encoding, written out independently of the service."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def expected_keys(workspace, goal, number, parent_run_key, criteria):
    request = digest(parent_run_key, "verify", criteria)
    return request, digest(workspace, goal, number, request, "verify")


def set_review(service):
    provider = service.providers["goal"]
    _, policy, ref, item = service.item("goal", 1)
    service.status(provider, policy, ref, item, "review")


def unprofiled_observations(service):
    """Drive launch, request, and acceptance for an item with no profile."""
    seen = {}
    work = launch(service)
    service.harness.sessions[work["run_key"]].status = "completed"
    item = service.providers["goal"].data
    wanted = expected_keys("test-workspace", "goal", 1, work["run_key"], BODY)

    # Caller: _verification_request.
    seen["request_matches_spec"] = (
        service._verification_request("goal", 1, work, item) == wanted
    )

    # Caller: launch. A request for other criteria is refused; the correct one
    # is accepted and becomes the stored verification run key.
    set_review(service)
    stale = expected_keys("test-workspace", "goal", 1, work["run_key"], "other")[0]
    try:
        service.launch(
            "goal",
            1,
            stale,
            5,
            actor=ACTOR,
            run_role="verify",
            parent_session_id=work["session_id"],
        )
        seen["launch_refuses_other_criteria"] = False
    except ValueError as exc:
        seen["launch_refuses_other_criteria"] = "current criteria" in str(exc)
    try:
        verify = service.launch(
            "goal",
            1,
            wanted[0],
            5,
            actor=ACTOR,
            run_role="verify",
            parent_session_id=work["session_id"],
        )
        seen["launch_accepts_spec_request"] = verify["run_key"] == wanted[1]
    except ValueError:
        seen["launch_accepts_spec_request"] = False
        return seen

    # Caller: update(action="verified"). Changed criteria invalidate; the
    # original criteria accept exactly once.
    service.harness.sessions[verify["run_key"]].status = "completed"
    passed = dict(behavioral_passed=True, checks_rerun=True, evidence_key="check:pass")
    item["body"] = "Amended public criteria"
    try:
        update(service, verify, "verified", **passed)
        seen["update_refuses_changed_criteria"] = False
    except ValueError as exc:
        seen["update_refuses_changed_criteria"] = "criteria changed" in str(exc)
    item["body"] = BODY
    try:
        outcome = update(service, verify, "verified", **passed)
        seen["update_accepts_original"] = outcome == {"outcome": "verified"}
    except ValueError:
        seen["update_accepts_original"] = False
    seen["closed_once"] = service.providers["goal"].closed == 1
    return seen


ALL_TRUE = {
    "request_matches_spec": True,
    "launch_refuses_other_criteria": True,
    "launch_accepts_spec_request": True,
    "update_refuses_changed_criteria": True,
    "update_accepts_original": True,
    "closed_once": True,
}


def test_unprofiled_launch_request_and_update_share_the_pinned_derivation(service):
    assert unprofiled_observations(service) == ALL_TRUE


def _drop_criteria(workspace, goal, number, parent, criteria):
    return expected_keys(workspace, goal, number, parent, None)


def _ignore_parent(workspace, goal, number, parent, criteria):
    return expected_keys(workspace, goal, number, "any-parent", criteria)


def _swap_goal_and_number(workspace, goal, number, parent, criteria):
    request = digest(parent, "verify", criteria)
    return request, digest(workspace, number, goal, request, "verify")


def _unsorted_compact_encoding(workspace, goal, number, parent, criteria):
    def compact(*parts):
        return hashlib.sha256(
            json.dumps(parts, separators=(",", ":")).encode()
        ).hexdigest()

    request = compact(parent, "verify", criteria)
    return request, compact(workspace, goal, number, request, "verify")


@pytest.mark.parametrize(
    "wrong,broken",
    [
        (_drop_criteria, "update_refuses_changed_criteria"),
        (_ignore_parent, "request_matches_spec"),
        (_swap_goal_and_number, "launch_accepts_spec_request"),
        (_unsorted_compact_encoding, "request_matches_spec"),
    ],
)
def test_seeded_wrong_derivation_changes_real_behaviour(
    service, monkeypatch, wrong, broken
):
    """Negative control: each wrong derivation must fail a named behaviour."""
    monkeypatch.setattr(service_module, "verification_identity", wrong)
    seen = unprofiled_observations(service)
    assert seen != ALL_TRUE
    assert seen[broken] is False


def test_null_empty_unicode_and_multiline_criteria_stay_distinct(service):
    work = launch(service)
    service.harness.sessions[work["run_key"]].status = "completed"
    item = service.providers["goal"].data
    keys = {}
    for label, body in [
        ("null", None),
        ("empty", ""),
        ("unicode", "Critère é ✓"),
        ("multiline", "line one\nline two"),
        ("trailing-newline", "line one\nline two\n"),
    ]:
        if body is None:
            item.pop("body", None)
        else:
            item["body"] = body
        got = service._verification_request("goal", 1, work, item)
        assert got == expected_keys("test-workspace", "goal", 1, work["run_key"], body)
        keys[label] = got
    assert len(set(keys.values())) == len(keys)


def test_profiled_acceptance_binds_to_resolved_source_not_issue_text(
    service, monkeypatch
):
    client, verify = ready(service, monkeypatch)
    profile = service.config.goals[0].github_acceptance_profiles["1"]
    work = service.store.links(goal_id="goal", number=1)[0]
    source = {
        "profile": profile.revision,
        "head": SHA,
        "branch": "main",
        "pull": 9,
        "repo": "example/project",
    }
    wanted = expected_keys("test-workspace", "goal", 1, work["run_key"], source)
    assert verify["run_key"] == wanted[1]
    item = service.providers["goal"].get_item(service.item("goal", 1)[2])
    assert service._verification_request("goal", 1, work, item) == wanted

    # A routine note outside the marked section changes no identity.
    client.body += "\nRoutine progress note."
    item = service.providers["goal"].get_item(service.item("goal", 1)[2])
    assert service._verification_request("goal", 1, work, item) == wanted

    # A stale provider profile is denied before any acceptance source read.
    current = service.providers["goal"].scope
    changed = current.model_dump()
    changed["github_acceptance_profiles"]["1"]["instructions"] = "Different."
    service.providers["goal"].scope = GoalScope.model_validate(changed)
    pull_reads = sum(path.endswith("/pulls/9") for _, path in client.reads)
    with pytest.raises(PermissionError, match="profile is stale"):
        verdict(service, verify)
    assert sum(path.endswith("/pulls/9") for _, path in client.reads) == pull_reads
    service.providers["goal"].scope = current

    # Substantive source movement invalidates; the original source accepts once.
    client.head = "b" * 40
    with pytest.raises(ValueError, match="criteria changed"):
        verdict(service, verify)
    assert client.closed == 0
    client.head = SHA
    assert verdict(service, verify) == {"outcome": "verified"}
    assert client.closed == 1


def test_deferred_local_file_verification_uses_the_same_derivation(
    tmp_path, pg_control_path
):
    """Callers: deferred completion and advance_deferred_verification."""
    scope, provider, harness, service, worker, work = _deferred_completion_service(
        tmp_path, pg_control_path
    )
    host = WorkActor("credential:host", "worker", host_id="host")
    assert service.update(
        scope.goal_id, 1, work["run_key"], "complete", actor=host
    ) == {"outcome": "deferred"}
    harness.sessions[work["run_key"]].turn_finished = True
    worker.process_once()
    verify = service.store.links(goal_id=scope.goal_id, number=1)[-1]
    assert (
        verify["run_key"]
        == expected_keys(
            "local-workspace", scope.goal_id, 1, work["run_key"], LOCAL_BODY
        )[1]
    )

    assert service.update(
        scope.goal_id,
        1,
        verify["run_key"],
        "verified",
        actor=host,
        behavioral_passed=True,
        checks_rerun=True,
        evidence_key="check:focused-pass",
    ) == {"outcome": "deferred"}
    harness.sessions[verify["run_key"]].turn_finished = True
    provider._change(_ref(), lambda item: item.__setitem__("body", "changed criteria"))
    assert service.advance_deferred_verification(scope.goal_id, 1) is None
    assert provider.get_item(_ref())["state"] == "open"
    provider._change(_ref(), lambda item: item.__setitem__("body", LOCAL_BODY))
    assert service.advance_deferred_verification(scope.goal_id, 1) == {
        "outcome": "verified"
    }
    assert provider.get_item(_ref())["state"] == "closed"


def test_seeded_wrong_derivation_is_visible_on_the_deferred_local_file_path(
    tmp_path, pg_control_path, monkeypatch
):
    monkeypatch.setattr(service_module, "verification_identity", _drop_criteria)
    scope, provider, harness, service, worker, work = _deferred_completion_service(
        tmp_path, pg_control_path
    )
    host = WorkActor("credential:host", "worker", host_id="host")
    service.update(scope.goal_id, 1, work["run_key"], "complete", actor=host)
    harness.sessions[work["run_key"]].turn_finished = True
    worker.process_once()
    verify = service.store.links(goal_id=scope.goal_id, number=1)[-1]
    service.update(
        scope.goal_id,
        1,
        verify["run_key"],
        "verified",
        actor=host,
        behavioral_passed=True,
        checks_rerun=True,
        evidence_key="check:focused-pass",
    )
    harness.sessions[verify["run_key"]].turn_finished = True
    provider._change(_ref(), lambda item: item.__setitem__("body", "changed criteria"))
    # The wrong derivation closes an item whose criteria changed: the exact
    # regression the real derivation prevents in the test above.
    assert service.advance_deferred_verification(scope.goal_id, 1) == {
        "outcome": "verified"
    }
