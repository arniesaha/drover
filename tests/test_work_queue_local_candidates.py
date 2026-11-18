import json
from types import SimpleNamespace

import pytest

from drover.server.work_queue.config import GoalScope, WorkQueueConfig
from drover.server.work_queue.github import ProviderUnavailable
from drover.server.work_queue.local_candidates import LocalCandidateProvider
from drover.server.work_queue.provider import ItemRef
from drover.server.work_queue.reconciler import QueueExecutionWorker, ShadowReconciler
from drover.server.work_queue.service import WorkService
from drover.server.work_queue.store import WorkStore


def _manifest(path, *, workspace="local-workspace", goal="goal-local"):
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "workspace_id": workspace,
                "goal_id": goal,
                "items": [
                    {
                        "number": 1,
                        "state": "open",
                        "body": "Perform only a harmless local canary effect.",
                        "created_at": "2026-11-18T00:00:00Z",
                        "labels": [
                            "goal:local",
                            "status:queued",
                            "agent:nix",
                        ],
                    }
                ],
            }
        )
    )


def _scope(path, **changes):
    return GoalScope.model_validate(
        {
            "goal_id": "goal-local",
            "repo": "example/project",
            "goal_label": "goal:local",
            "candidate_source": "local_file",
            "local_candidates_path": str(path),
            "credential": {
                "token_env": "UNUSED_LOCAL_CANDIDATE_TOKEN",
                "capabilities": ["issues_read"],
            },
            **changes,
        }
    )


def _ref():
    return ItemRef(
        "local-workspace", "local_file", "example/project", 1, "local://goal-local/1"
    )


def test_local_candidate_manifest_is_bound_and_status_writes_are_atomic(tmp_path):
    path = tmp_path / "candidates.json"
    _manifest(path)
    provider = LocalCandidateProvider("local-workspace", _scope(path))

    provider.authenticate()
    item = provider.get_item(_ref())
    assert item["labels"][-1] == {"name": "agent:nix"}
    assert item["operator_authority_labels"] == [
        "goal:local",
        "status:queued",
        "agent:nix",
    ]
    provider.set_labels(_ref(), ["goal:local", "status:running", "agent:nix"])

    saved = json.loads(path.read_text())
    assert saved["items"][0]["labels"] == [
        "goal:local",
        "status:running",
        "agent:nix",
    ]
    assert not path.with_name(path.name + ".tmp").exists()


def test_local_candidate_manifest_rejects_cross_scope_and_malformed_content(tmp_path):
    path = tmp_path / "candidates.json"
    _manifest(path, workspace="other-workspace")
    provider = LocalCandidateProvider("local-workspace", _scope(path))
    with pytest.raises(ProviderUnavailable, match="manifest_invalid"):
        provider.authenticate()

    _manifest(path)
    with pytest.raises(PermissionError, match="cross-workspace"):
        provider.get_item(
            ItemRef(
                "local-workspace",
                "github",
                "example/project",
                1,
                "local://goal-local/1",
            )
        )


def test_local_candidate_config_fails_closed_without_absolute_manifest(tmp_path):
    with pytest.raises(ValueError, match="absolute manifest"):
        _scope(tmp_path / "relative").model_copy(
            update={"local_candidates_path": "relative.json"}
        ).valid()
    with pytest.raises(ValueError, match="positive item keys"):
        _scope(tmp_path / "candidates.json", local_candidate_worktrees={"x": "/tmp"})
    with pytest.raises(ValueError, match="cannot configure local"):
        GoalScope.model_validate(
            {
                "goal_id": "github-goal",
                "repo": "example/project",
                "goal_label": "goal:github",
                "local_candidate_worktrees": {"1": "/tmp"},
                "credential": {
                    "token_env": "UNUSED_LOCAL_CANDIDATE_TOKEN",
                    "capabilities": ["issues_read"],
                },
            }
        )


def test_local_candidate_reconciler_and_work_service_share_the_same_provider(tmp_path):
    path = tmp_path / "candidates.json"
    _manifest(path)
    scope = _scope(path)
    config = WorkQueueConfig(
        enabled=True,
        shadow_mode=False,
        workspace_id="local-workspace",
        goals=[scope],
    )
    provider = LocalCandidateProvider(config.workspace_id, scope)
    report = ShadowReconciler(
        config, service_principal="control:queue-execution"
    ).poll()
    assert [row["number"] for row in report["actions"]] == [1]

    service = SimpleNamespace(config=config, providers={scope.goal_id: provider})
    worker = QueueExecutionWorker(service)
    assert worker.reconciler.providers is service.providers
    work = WorkService(config, None, service.providers, None)
    assert work.ref(scope, 1) == _ref()


def test_queue_worker_launches_an_explicit_local_candidate_through_work_service(
    tmp_path, pg_control_path
):
    path = tmp_path / "candidates.json"
    _manifest(path)
    scope = _scope(
        path,
        execution_enabled=True,
        execution_cwds={"host": str(tmp_path)},
        brief_ref="docs/local-canary.md",
        continuity_owner_id="drover:owner/goal-local",
        continuity_owner_harness="codex",
        local_candidate_worktrees={"1": str(tmp_path / "worktree-1")},
    )
    config = WorkQueueConfig(
        enabled=True,
        shadow_mode=False,
        workspace_id="local-workspace",
        execution={"global_max_workers": 2},
        goals=[scope],
    )

    class Harness:
        def __init__(self):
            self.sessions = {}
            self.launches = []

        def active(self, links, *, release_finalised=True):
            return [
                row
                for row in links
                if not row["session_id"]
                or self.sessions[row["run_key"]].status == "running"
            ]

        def resolve(self, run_key):
            return self.sessions.get(run_key)

        def quota(self):
            return [
                {
                    "provider": "openai",
                    "status": "ok",
                    "source": "codex-app-server",
                    "harnesses": ["codex"],
                    "hosts": [{"host_id": "host"}],
                    "windows": [{"kind": "primary", "status": "ok", "used_percent": 1}],
                }
            ]

        def eligible(self, goal_id=None, parent_session_id=None):
            return {("host", "codex")}

        def launch(self, host, harness, run_key, ref, role, parent, goal_id, **kwargs):
            self.launches.append((host, harness, ref, role, goal_id, kwargs))
            self.sessions[run_key] = SimpleNamespace(
                session_id="local-session-1",
                status="running",
                host_id=host,
                parent_session_id=parent,
            )
            return "local-session-1"

    provider = LocalCandidateProvider(config.workspace_id, scope)
    harness = Harness()
    service = WorkService(
        config,
        WorkStore(pg_control_path, config.workspace_id),
        {scope.goal_id: provider},
        harness,
    )
    QueueExecutionWorker(service).process_once()

    link = service.store.links(goal_id=scope.goal_id, number=1)[0]
    assert link["session_id"] == "local-session-1"
    assert harness.launches[0][2] == _ref()
    assert harness.launches[0][4] == scope.goal_id
    assert harness.launches[0][5]["authority"]["local_candidate_cwd"] == str(
        tmp_path / "worktree-1"
    )
    assert provider.get_item(_ref())["labels"] == [
        {"name": "goal:local"},
        {"name": "agent:nix"},
        {"name": "status:running"},
    ]
