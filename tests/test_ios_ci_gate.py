"""Release evidence must come from ios.yml main-push checks on the same SHA."""

from copy import deepcopy
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest
from ios.require_ios_ci import GitHub, Rejected, require_ios_ci

SHA = "a" * 40
OTHER_SHA = "b" * 40
REPOSITORY = "example/drover"
CHECK = {
    "name": "Build and test iOS app",
    "head_sha": SHA,
    "check_suite": {"id": 42},
    "app": {"slug": "github-actions"},
    "status": "completed",
    "conclusion": "success",
}
RUN = {
    "id": 123,
    "check_suite_id": 42,
    "path": ".github/workflows/ios.yml",
    "head_sha": SHA,
    "head_branch": "main",
    "event": "push",
    "status": "completed",
    "conclusion": "success",
    "run_attempt": 1,
}


def evidence(runs=None, checks=None):
    calls = []

    def get(path):
        calls.append(path)
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/actions/workflows/ios.yml/runs"):
            assert query["head_sha"] == [SHA]
            assert query["event"] == ["push"]
            assert query["branch"] == ["main"]
            values, field = runs if runs is not None else [RUN], "workflow_runs"
        else:
            assert parsed.path == f"/repos/{REPOSITORY}/check-suites/42/check-runs"
            assert query["filter"] == ["latest"]
            values, field = checks if checks is not None else [CHECK], "check_runs"
        page = int(query["page"][0])
        return {
            "total_count": len(values),
            field: values[(page - 1) * 100 : page * 100],
        }

    return get, calls


def test_exact_sha_main_push_checks_pass():
    get, calls = evidence()
    assert require_ios_ci(get, REPOSITORY, SHA) == [123]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"head_sha": OTHER_SHA, "head_commit": {"tree_id": "same-tree"}},
        {"event": "pull_request"},
        {"event": "workflow_dispatch"},
        {"head_branch": "other-branch"},
        {"path": ".github/workflows/ci.yml"},
    ],
)
def test_pr_equal_tree_wrong_sha_branch_event_or_workflow_cannot_satisfy_gate(change):
    get, _ = evidence(runs=[RUN | change])
    with pytest.raises(
        Rejected,
        match="missing ios.yml.*exact SHA.*missing checks: Build and test iOS app",
    ):
        require_ios_ci(get, REPOSITORY, SHA)


def test_missing_workflow_or_required_check_fails_with_name():
    for get, _ in (
        evidence(runs=[]),
        evidence(checks=[]),
        evidence(checks=[CHECK | {"name": "other check"}]),
    ):
        with pytest.raises(Rejected, match="missing.*Build and test iOS app"):
            require_ios_ci(get, REPOSITORY, SHA)


@pytest.mark.parametrize(
    "conclusion",
    [
        "failure",
        "cancelled",
        "skipped",
        "neutral",
        "timed_out",
        "action_required",
        None,
    ],
)
def test_every_check_must_conclude_success(conclusion):
    get, _ = evidence(checks=[CHECK | {"conclusion": conclusion}])
    with pytest.raises(Rejected, match="Build and test iOS app"):
        require_ios_ci(get, REPOSITORY, SHA)


def test_pending_check_and_run_fail_without_waiting():
    get, calls = evidence(
        runs=[RUN | {"status": "in_progress", "conclusion": None}],
        checks=[CHECK | {"status": "queued", "conclusion": None}],
    )
    with pytest.raises(Rejected, match="Build and test iOS app.*queued/None"):
        require_ios_ci(get, REPOSITORY, SHA)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"head_sha": OTHER_SHA},
        {"check_suite": {"id": 99}},
        {"app": {"slug": "other-app"}},
    ],
)
def test_check_identity_is_verified(change):
    get, _ = evidence(checks=[CHECK | change])
    with pytest.raises(
        Rejected, match="Build and test iOS app.*wrong SHA, suite or check owner"
    ):
        require_ios_ci(get, REPOSITORY, SHA)


def test_failure_in_additional_check_on_later_page_is_not_ignored():
    checks = [CHECK] + [CHECK | {"name": f"matrix {n}"} for n in range(100)]
    checks[-1] |= {"conclusion": "failure"}
    get, calls = evidence(checks=checks)
    with pytest.raises(Rejected, match="matrix 99.*failure"):
        require_ios_ci(get, REPOSITORY, SHA)
    assert len(calls) == 3


def test_all_matching_push_runs_must_succeed_not_only_one():
    get, _ = evidence(runs=[RUN, RUN | {"id": 124, "conclusion": "cancelled"}])
    with pytest.raises(Rejected, match="run 124.*cancelled"):
        require_ios_ci(get, REPOSITORY, SHA)


def test_rerun_uses_latest_checks_not_failed_historical_attempts():
    get, _ = evidence(runs=[RUN | {"run_attempt": 2}])
    assert require_ios_ci(get, REPOSITORY, SHA) == [123]


def test_incomplete_paginated_results_fail_closed():
    get, _ = evidence()

    def incomplete(path):
        result = deepcopy(get(path))
        if "check-runs" in path:
            result["total_count"] = 2
            if "page=2" in path:
                result["check_runs"] = []
        return result

    with pytest.raises(Rejected, match="incomplete"):
        require_ios_ci(incomplete, REPOSITORY, SHA)


def test_api_permission_failure_does_not_print_token_or_response(monkeypatch):
    from ios import require_ios_ci as gate

    def denied(request, timeout):
        assert request.get_header("Authorization") == "Bearer private-token"
        raise HTTPError(request.full_url, 403, "private-response", {}, None)

    monkeypatch.setattr(gate, "urlopen", denied)
    with pytest.raises(Rejected, match="HTTP 403") as error:
        GitHub("private-token").get(
            "/repos/example/drover/actions/workflows/ios.yml/runs"
        )
    assert "private-token" not in str(error.value)
    assert "private-response" not in str(error.value)
