"""A release may only publish a commit whose main ci.yml push run is green."""

from urllib.parse import parse_qs, urlsplit

import pytest
from ios.require_ios_ci import Rejected
from require_main_ci import Pending, require_main_ci, wait_for_main_ci

SHA = "a" * 40
REPOSITORY = "example/drover"
RUN = {
    "id": 7,
    "run_number": 7,
    "path": ".github/workflows/ci.yml",
    "head_sha": SHA,
    "head_branch": "main",
    "event": "push",
    "status": "completed",
    "conclusion": "success",
}


def api(*responses):
    """Serve successive ci.yml run listings, repeating the last one."""
    calls = []

    def get(path):
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        assert parsed.path == f"/repos/{REPOSITORY}/actions/workflows/ci.yml/runs"
        assert query["head_sha"] == [SHA]
        assert query["event"] == ["push"]
        assert query["branch"] == ["main"]
        runs = responses[min(len(calls), len(responses) - 1)]
        calls.append(path)
        return {"total_count": len(runs), "workflow_runs": runs}

    return get, calls


def test_green_main_push_run_passes():
    get, _ = api([RUN])
    assert require_main_ci(get, REPOSITORY, SHA) == [7]


@pytest.mark.parametrize(
    "change",
    [
        {"event": "pull_request"},
        {"head_branch": "feature"},
        {"path": ".github/workflows/ios.yml"},
        {"head_sha": "b" * 40},
    ],
)
def test_runs_that_are_not_main_push_ci_for_the_commit_do_not_count(change):
    get, _ = api([RUN | change])
    with pytest.raises(Pending, match="no ci.yml push run"):
        require_main_ci(get, REPOSITORY, SHA)


def test_red_ci_is_rejected():
    get, _ = api([RUN | {"conclusion": "failure"}])
    with pytest.raises(Rejected, match="concluded failure") as raised:
        require_main_ci(get, REPOSITORY, SHA)
    assert not isinstance(raised.value, Pending)


def test_newest_run_decides():
    green_rerun = RUN | {"id": 9, "run_number": 9}
    red_original = RUN | {"conclusion": "failure"}
    get, _ = api([red_original, green_rerun])
    assert require_main_ci(get, REPOSITORY, SHA) == [9]

    get, _ = api([RUN, RUN | {"id": 9, "run_number": 9, "conclusion": "failure"}])
    with pytest.raises(Rejected, match="concluded failure"):
        require_main_ci(get, REPOSITORY, SHA)


def test_in_progress_ci_is_waited_for_then_accepted():
    get, calls = api([RUN | {"status": "in_progress", "conclusion": None}], [RUN])
    sleeps = []
    assert wait_for_main_ci(
        get, REPOSITORY, SHA, wait_seconds=600, sleep=sleeps.append, clock=lambda: 0
    ) == [7]
    assert len(calls) == 2 and len(sleeps) == 1


def test_waiting_is_bounded():
    get, _ = api([RUN | {"status": "queued", "conclusion": None}])
    now = iter([0, 0, 1000])
    with pytest.raises(Rejected, match="timed out waiting for CI"):
        wait_for_main_ci(
            get,
            REPOSITORY,
            SHA,
            wait_seconds=60,
            sleep=lambda _: None,
            clock=lambda: next(now),
        )


@pytest.mark.parametrize(
    "repository, sha", [("not a repo", SHA), (REPOSITORY, "abc123")]
)
def test_malformed_identity_is_rejected(repository, sha):
    get, _ = api([RUN])
    with pytest.raises(Rejected):
        require_main_ci(get, repository, sha)
