import json
import threading
from dataclasses import replace

import pytest
from test_work_queue_policy import scope

from drover.server.work_queue.github import GitHubProvider, ProviderUnavailable
from drover.server.work_queue.provider import ItemRef
from drover import github_app_token


class Response:
    def __init__(self, data=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = json.dumps(data).encode() if data is not None else b""
        self.data = data

    def json(self):
        return self.data


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class Routed(Session):
    """Answers by URL path, for reads that run concurrently."""

    def __init__(self, routes):
        super().__init__()
        self.routes = routes
        self.lock = threading.Lock()

    def request(self, method, url, **kwargs):
        with self.lock:
            self.calls.append((method, url, kwargs))
        target = url + "?"
        return next(r for key, r in self.routes.items() if key in target)


@pytest.fixture
def credential(monkeypatch):
    monkeypatch.setenv("TEST_WORK_QUEUE_TOKEN", "synthetic-test-token")


def ref():
    return ItemRef(
        "workspace-test",
        "github",
        "example/project",
        7,
        "https://github.com/example/project/issues/7",
    )


def test_pagination_conditional_reads_and_pr_exclusion(credential):
    page = [{"number": n} for n in range(100)]
    session = Session(
        Response(page, headers={"ETag": '"first"'}),
        Response([{"number": 101}, {"number": 102, "pull_request": {}}]),
        Response(status=304),
        Response([{"number": 101}, {"number": 102, "pull_request": {}}]),
    )
    provider = GitHubProvider("workspace-test", scope(), session=session)
    assert len(provider.list_items()) == 101
    assert provider.list_items() == page + [{"number": 101}]
    assert session.calls[1][2]["params"]["page"] == 2
    assert session.calls[2][2]["headers"]["If-None-Match"] == '"first"'
    assert session.calls[0][2]["params"]["labels"] == "goal:2026-w42"
    assert all(c[0] == "GET" for c in session.calls)


def test_cache_is_bounded_expires_and_rotation_clears_identity(credential, monkeypatch):
    now = [10.0]
    session = Session(
        *[Response({"id": n}, headers={"ETag": str(n)}) for n in range(131)]
    )
    provider = GitHubProvider(
        "workspace-test", scope(), session=session, clock=lambda: now[0]
    )
    for n in range(129):
        provider._request("GET", f"/test/{n}")
    assert len(provider._cache) == 128
    now[0] += 301
    provider._request("GET", "/test/128")
    assert "If-None-Match" not in session.calls[-1][2]["headers"]
    provider.actor_principal = "old-principal"
    monkeypatch.setenv("TEST_WORK_QUEUE_TOKEN", "rotated-synthetic-token")
    provider._request("GET", "/user")
    assert provider.actor_principal == ""
    assert len(provider._cache) == 1


def test_backoff_and_sanitized_failure(credential):
    now = [10.0]
    session = Session(
        Response({"private": "not-for-report"}, 429, {"Retry-After": "30"}),
        Response([]),
    )
    provider = GitHubProvider(
        "workspace-test", scope(), session=session, clock=lambda: now[0]
    )
    with pytest.raises(ProviderUnavailable, match="rate_limit_backoff"):
        provider.list_items()
    with pytest.raises(ProviderUnavailable, match="rate_limit_backoff"):
        provider.list_items()
    assert len(session.calls) == 1
    now[0] = 40
    assert provider.list_items() == []


def test_token_and_app_identity_repository_check(credential):
    provider = GitHubProvider(
        "workspace-test", scope(), session=Session(Response({"id": 42}))
    )
    provider.authenticate()
    assert provider.actor_principal == "github:user:42"
    app = scope(
        credential={
            "mode": "app",
            "token_env": "TEST_WORK_QUEUE_TOKEN",
            "installation_id": 3,
            "repository_id": 4,
            "capabilities": ["issues_read"],
        }
    )
    provider = GitHubProvider(
        "workspace-test",
        app,
        session=Session(
            Response({"repositories": [{"id": 4, "full_name": "example/project"}]})
        ),
    )
    provider.authenticate()
    assert provider.actor_principal == "github:installation:3"
    provider = GitHubProvider(
        "workspace-test",
        app,
        session=Session(
            Response({"repositories": [{"id": 5, "full_name": "example/project"}]})
        ),
    )
    with pytest.raises(ProviderUnavailable, match="repository_mismatch"):
        provider.authenticate()


def test_reads_labels_milestones_comments_pulls_checks_and_environment_gates(
    credential,
):
    sha = "a" * 40
    session = Session(
        Response([]),
        Response([]),
        Response([]),
        Response({"number": 7}),
        Response({"check_runs": [{"conclusion": "failure"}]}),
    )
    provider = GitHubProvider("workspace-test", scope(), session=session)
    assert (
        provider.list_labels()
        == provider.list_milestones()
        == provider.list_comments(ref())
        == []
    )
    assert provider.get_pull(ref())["number"] == 7
    assert provider.list_checks(sha)[0]["conclusion"] == "failure"
    # Attention reads run concurrently, so responses are routed by URL.
    session = Routed(
        {
            "/pulls?": Response(
                [
                    {
                        "number": 8,
                        "head": {"sha": sha},
                        "draft": True,
                        "html_url": "https://github.com/example/project/pull/8",
                        "updated_at": "2026-10-13T00:00:00Z",
                    }
                ]
            ),
            "/check-runs?": Response(
                {"check_runs": [{"id": 11, "conclusion": "failure"}]}
            ),
            "/actions/runs?": Response(
                {
                    "workflow_runs": [
                        {
                            "id": 9,
                            "run_attempt": 2,
                            "html_url": "https://github.com/example/project/actions/runs/9",
                            "updated_at": "2026-10-13T00:00:00Z",
                        }
                    ]
                }
            ),
            "/pending_deployments?": Response(
                [{"environment": {"id": 10}, "current_user_can_approve": True}]
            ),
        }
    )
    provider.session = session
    attention = provider.list_attention()
    assert [r["kind"] for r in attention] == [
        "draft_pr",
        "failed_check",
        "environment_approval",
    ]
    assert attention[2]["source_key"] == "environment:9:2:10"
    assert attention[2]["actions"] == ["open"]
    assert all(c[0] == "GET" for c in session.calls)
    assert len(session.calls) == 4
    waiting = [c for c in session.calls if c[1].endswith("/actions/runs")]
    assert waiting[0][2]["params"]["status"] == "waiting"


def test_cross_workspace_refs_missing_permissions_and_public_text_fail_closed(
    credential,
):
    session = Session()
    provider = GitHubProvider("workspace-test", scope(), session=session)
    for wrong in (
        replace(ref(), workspace_id="other"),
        replace(ref(), repo="other/project"),
    ):
        with pytest.raises(PermissionError):
            provider.get_item(wrong)
    with pytest.raises(ProviderUnavailable, match="issues_write"):
        provider.close(ref())
    with pytest.raises(PermissionError, match="sanitization"):
        provider.comment(ref(), "private context")
    with pytest.raises(PermissionError, match="phase 2"):
        provider.execute_action(ref(), "approve")
    assert not provider.inspect_action(ref(), "approve")["supported"]
    assert session.calls == []


def test_missing_credentials_never_call_provider(monkeypatch):
    monkeypatch.delenv("TEST_WORK_QUEUE_TOKEN", raising=False)
    session = Session()
    provider = GitHubProvider("workspace-test", scope(), session=session)
    with pytest.raises(ProviderUnavailable, match="credential_unavailable"):
        provider.list_items()
    assert not session.calls


def test_authority_labels_follow_the_latest_label_event(credential):
    item = {
        "number": 7,
        "labels": [
            {"name": "autonomy:through-pr"},
            {"name": "autonomy:through-merge"},
            {"name": "autonomy:through-rollout"},
        ],
    }
    events = [
        {
            "event": "labeled",
            "label": {"name": "autonomy:through-pr"},
            "actor": {"login": "operator", "type": "User"},
        },
        {
            "event": "labeled",
            "label": {"name": "autonomy:through-pr"},
            "actor": {"login": "drover-bot[bot]", "type": "Bot"},
        },
        {
            "event": "labeled",
            "label": {"name": "autonomy:through-merge"},
            "actor": {"login": "operator", "type": "User"},
        },
        {
            "event": "unlabeled",
            "label": {"name": "autonomy:through-merge"},
            "actor": {"login": "operator", "type": "User"},
        },
        {
            "event": "labeled",
            "label": {"name": "autonomy:through-rollout"},
            "actor": {"login": "operator", "type": "User"},
        },
    ]
    provider = GitHubProvider(
        "workspace-test", scope(), session=Session(Response(item), Response(events))
    )
    assert provider.get_item(ref())["operator_authority_labels"] == [
        "autonomy:through-rollout"
    ]


def test_write_uses_packaged_app_token_or_falls_back_when_unconfigured(
    credential, monkeypatch
):
    write_scope = scope(
        credential={
            "token_env": "TEST_WORK_QUEUE_TOKEN",
            "capabilities": ["issues_read", "issues_write"],
        }
    )
    monkeypatch.setattr(github_app_token, "load_config", lambda: {})
    fallback = GitHubProvider(
        "workspace-test", write_scope, session=Session(Response({"id": 42}), Response())
    )
    fallback.close(ref())
    assert fallback.session.calls[-1][2]["headers"]["Authorization"] == (
        "Bearer synthetic-test-token"
    )

    monkeypatch.setattr(github_app_token, "load_config", lambda: {"app": "configured"})
    monkeypatch.setattr(
        github_app_token,
        "mint",
        lambda config, repo: {"token": "app-installation-token"},
    )
    app = GitHubProvider(
        "workspace-test", write_scope, session=Session(Response({"id": 42}), Response())
    )
    app.close(ref())
    assert app.session.calls[-1][2]["headers"]["Authorization"] == (
        "Bearer app-installation-token"
    )


def test_configured_app_mint_failure_never_falls_back_to_operator_token(
    credential, monkeypatch
):
    write_scope = scope(
        credential={
            "token_env": "TEST_WORK_QUEUE_TOKEN",
            "capabilities": ["issues_read", "issues_write"],
        }
    )
    monkeypatch.setattr(github_app_token, "load_config", lambda: {"app": "configured"})
    monkeypatch.setattr(
        github_app_token,
        "mint",
        lambda config, repo: (_ for _ in ()).throw(github_app_token.MintError("no")),
    )
    provider = GitHubProvider(
        "workspace-test", write_scope, session=Session(Response({"id": 42}))
    )
    with pytest.raises(ProviderUnavailable, match="app_token_unavailable"):
        provider.close(ref())
    assert len(provider.session.calls) == 1
