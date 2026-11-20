import json
from types import SimpleNamespace

import pytest

from drover.server.work_queue.config import GoalScope, WorkQueueConfig
from drover.server.work_queue.github import ProviderUnavailable
from drover.server.work_queue.local_candidates import LocalCandidateProvider
from drover.server.work_queue.provider import ItemRef
from drover.server.work_queue.reconciler import QueueExecutionWorker, ShadowReconciler
from drover.server.work_queue.runtime import HarnessBridge
from drover.server.work_queue.service import WorkDenied, WorkService, identity
from drover.server.work_queue.store import WorkStore


def _manifest(
    path, *, workspace="local-workspace", goal="goal-local", expected_effect=None
):
    item = {
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
    if expected_effect is not None:
        item["expected_effect_artifact"] = expected_effect
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "workspace_id": workspace,
                "goal_id": goal,
                "items": [item],
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


def test_local_candidate_effect_reference_is_explicit_and_bounded(tmp_path):
    path = tmp_path / "candidates.json"
    expected = "local-file:.drover-native-canary/source-task-9.json"
    _manifest(path, expected_effect=expected)
    provider = LocalCandidateProvider("local-workspace", _scope(path))
    assert provider.get_item(_ref())["expected_effect_artifact"] == expected

    for invalid in (
        " ",
        "worktrees/work-b/.drover-native-canary/source-task-9.json",
        "local-file:worktrees/work-b/.drover-native-canary/source-task-9.json",
        "local-file:.drover-native-canary/../generic.json",
    ):
        _manifest(path, expected_effect=invalid)
        with pytest.raises(ProviderUnavailable, match="manifest_invalid"):
            provider.authenticate()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"materialized": False, "verification": "absent"}, False),
        ({"materialized": True, "verification": "materialized"}, True),
        ({"materialized": False}, None),
        ({"materialized": False, "verification": "unknown"}, None),
    ],
)
def test_bridge_requires_explicit_absent_effect_verification(payload, expected):
    bridge = object.__new__(HarnessBridge)
    bridge.resolve = lambda run_key: SimpleNamespace(
        session_id="coordinator", handoff_mode="work_coordinator"
    )
    bridge.registry = SimpleNamespace(
        work_coordinator_expected_effect_artifact=lambda session_id, run_id: (
            "local-file:.drover-native-canary/source-task-1.json"
        )
    )
    bridge.collector = SimpleNamespace(
        proxy_harness_session_action=lambda session_id, action, body: (
            200,
            json.dumps(payload),
        )
    )
    assert (
        bridge.expected_effect_materialized(
            "run", "local-file:.drover-native-canary/source-task-1.json"
        )
        is expected
    )


def test_bridge_refuses_current_manifest_artifact_that_differs_from_signed_binding():
    bridge = object.__new__(HarnessBridge)
    bridge.resolve = lambda run_key: SimpleNamespace(
        session_id="coordinator", handoff_mode="work_coordinator"
    )
    bridge.registry = SimpleNamespace(
        work_coordinator_expected_effect_artifact=lambda session_id, run_id: (
            "local-file:.drover-native-canary/source-task-original.json"
        )
    )
    called = []
    bridge.collector = SimpleNamespace(
        proxy_harness_session_action=lambda *args: called.append(args)
    )

    assert (
        bridge.expected_effect_materialized(
            "run", "local-file:.drover-native-canary/source-task-changed.json"
        )
        is None
    )
    assert called == []


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


def test_local_candidate_config_accepts_isolated_worktrees_and_selected_fence(tmp_path):
    manifest = tmp_path / "candidates.json"
    worktree = tmp_path / "worktree-1"
    scope = _scope(
        manifest,
        local_candidate_worktrees={"1": str(worktree)},
        integration_serialization_key="canary-write",
        integration_serialization_label="delivery:integration",
        continuity_owner_id="drover:owner/goal-local",
        execution_enabled=True,
        execution_cwds={"host": str(worktree)},
        brief_ref="docs/local-canary.md",
        continuity_owner_harness="codex",
    )
    assert scope.local_candidate_worktrees == {"1": str(worktree)}
    assert scope.integration_serialization_label == "delivery:integration"

    with pytest.raises(ValueError, match="label requires a serialization key"):
        _scope(manifest, integration_serialization_label="delivery:integration")


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


def test_bridge_delivers_unmapped_local_candidate_task_to_assigned_worktree(tmp_path):
    """The daemon-assigned worktree, not an optional static map, authorizes work."""
    manifest = tmp_path / "candidates.json"
    _manifest(manifest)
    expected = "local-file:.drover-native-canary/source-task-19.json"
    task = (
        "Write .drover-native-canary/source-task-19.json in the assigned "
        "disposable worktree."
    )
    scope = _scope(
        manifest,
        execution_enabled=True,
        execution_cwds={"host": str(tmp_path / "configured-checkout")},
        brief_ref="docs/local-canary.md",
        continuity_owner_id="drover:owner/goal-local",
        continuity_owner_harness="codex",
        # Candidate 19 is deliberately absent: this optional map picks a
        # launch checkout, not whether the daemon may assign a worktree.
        local_candidate_worktrees={"1": str(tmp_path / "worktree-1")},
    )
    config = WorkQueueConfig(
        enabled=True,
        shadow_mode=False,
        workspace_id="local-workspace",
        goals=[scope],
    )
    payloads = []
    bridge = object.__new__(HarnessBridge)
    bridge.config = config
    bridge.collector = SimpleNamespace(
        work_coordinator_continuity_secret="test-continuity-secret",
        proxy_create_harness_session=lambda host, payload: (
            payloads.append((host, payload)) or (201, '{"session_id":"worker-19"}')
        ),
    )
    bridge.registry = SimpleNamespace()

    assert (
        bridge.launch(
            "host",
            "codex",
            "a" * 32,
            ItemRef(
                "local-workspace",
                "local_file",
                "example/project",
                19,
                "local://goal-local/19",
            ),
            "work",
            None,
            scope.goal_id,
            authority={
                "labels": ["goal:local", "status:queued"],
                "body": task,
                "local_candidate_number": 19,
                "local_candidate_cwd": None,
                "expected_effect_artifact": expected,
            },
        )
        == "worker-19"
    )
    _host, payload = payloads[0]
    assert payload["cwd"] == str(tmp_path / "configured-checkout")
    assert task in payload["prompt"]
    assert "Assigned isolated worktree" in payload["prompt"]
    assert "missing entry is not a worker authorization gate" in payload["prompt"]
    assert (
        payload["work_coordinator_continuity"]["expected_effect_artifact"] == expected
    )


def test_bridge_refuses_local_candidate_without_task_authority(tmp_path):
    manifest = tmp_path / "candidates.json"
    _manifest(manifest)
    scope = _scope(
        manifest,
        execution_enabled=True,
        execution_cwds={"host": str(tmp_path)},
        brief_ref="docs/local-canary.md",
        continuity_owner_id="drover:owner/goal-local",
        continuity_owner_harness="codex",
    )
    bridge = object.__new__(HarnessBridge)
    bridge.config = WorkQueueConfig(
        enabled=True,
        shadow_mode=False,
        workspace_id="local-workspace",
        goals=[scope],
    )
    bridge.registry = SimpleNamespace()
    bridge.collector = SimpleNamespace(
        proxy_create_harness_session=lambda *_: pytest.fail("invalid task launched")
    )

    with pytest.raises(WorkDenied, match="local candidate task authority is invalid"):
        bridge.launch(
            "host",
            "codex",
            "a" * 32,
            _ref(),
            "work",
            None,
            scope.goal_id,
            authority={"local_candidate_number": 1, "body": " "},
        )


def test_queue_worker_launches_an_explicit_local_candidate_through_work_service(
    tmp_path, pg_control_path
):
    path = tmp_path / "candidates.json"
    expected = "local-file:.drover-native-canary/source-task-1.json"
    _manifest(path, expected_effect=expected)
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
    assert harness.launches[0][5]["authority"]["expected_effect_artifact"] == expected
    assert provider.get_item(_ref())["labels"] == [
        {"name": "goal:local"},
        {"name": "agent:nix"},
        {"name": "status:running"},
    ]


def _terminal_local_worker_service(
    tmp_path,
    pg_control_path,
    *,
    category,
    materialized,
    provider_artifact=None,
    bound_artifact=None,
):
    path = tmp_path / "candidates.json"
    expected = "local-file:.drover-native-canary/source-task-1.json"
    _manifest(path, expected_effect=provider_artifact or expected)
    manifest = json.loads(path.read_text())
    manifest["items"][0]["labels"] = [
        "goal:local",
        "status:running",
        "agent:nix",
    ]
    path.write_text(json.dumps(manifest))
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
        goals=[scope],
    )

    class Harness:
        def __init__(self):
            self.bound_artifact = bound_artifact or expected
            self.sessions = {
                "terminal-run": SimpleNamespace(
                    session_id="terminal-worker",
                    status="completed",
                    handoff_mode="work_coordinator",
                )
            }
            self.launches = []

        def resolve(self, run_key):
            return self.sessions.get(run_key)

        def active(self, links, *, release_finalised=True):
            return []

        def coordinator_failure_category(self, run_key):
            assert run_key in self.sessions
            return category

        def expected_effect_materialized(self, run_key, artifact):
            assert run_key in self.sessions
            if artifact != self.bound_artifact:
                return None
            return materialized

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
            self.launches.append(run_key)
            self.sessions[run_key] = SimpleNamespace(
                session_id="terminal-successor",
                status="completed",
                handoff_mode="work_coordinator",
            )
            return "terminal-successor"

    provider = LocalCandidateProvider(config.workspace_id, scope)
    store = WorkStore(pg_control_path, config.workspace_id)
    harness = Harness()
    service = WorkService(config, store, {scope.goal_id: provider}, harness)
    store.intent(
        "control:queue-execution",
        _ref(),
        scope.goal_id,
        "terminal-run",
        "work",
        0,
        scope.goal_label,
    )
    store.associate("terminal-run", "terminal-worker")
    return path, scope, service, store


def test_queue_worker_rearms_only_typed_pre_effect_worker_auth_failure(
    tmp_path, pg_control_path
):
    path, scope, service, store = _terminal_local_worker_service(
        tmp_path,
        pg_control_path,
        category="authentication_required",
        materialized=False,
    )
    worker = QueueExecutionWorker(service)

    worker.process_once()
    assert [
        label
        for label in json.loads(path.read_text())["items"][0]["labels"]
        if label.startswith("status:")
    ] == ["status:queued"]
    recovery_key = identity(
        "automatic-work-coordinator-auth-recovery", scope.goal_id, "1"
    )
    recovery_events = store.events(recovery_key)
    assert [(event["event_kind"], event["outcome"]) for event in recovery_events] == [
        ("intent", None),
        ("outcome", "queued"),
    ]

    # The next ordinary queue pass creates a successor with its inherited
    # failure count. A second typed terminal failure remains running: the
    # lineage-wide journal allows exactly one automatic recovery.
    worker.process_once()
    links = store.links(goal_id=scope.goal_id, number=1)
    assert len(links) == 2
    assert links[-1]["failure_count"] == 1
    assert [
        label
        for label in json.loads(path.read_text())["items"][0]["labels"]
        if label.startswith("status:")
    ] == ["status:running"]
    worker.process_once()
    assert len(store.events(recovery_key)) == 2
    assert [
        label
        for label in json.loads(path.read_text())["items"][0]["labels"]
        if label.startswith("status:")
    ] == ["status:running"]


@pytest.mark.parametrize(
    ("category", "materialized"),
    [
        (None, False),
        ("authentication_required", True),
        ("authentication_required", None),
    ],
    ids=[
        "human-or-prose-block",
        "materialized-candidate-effect",
        "unknown-effect-readback",
    ],
)
def test_queue_worker_never_rearms_human_or_materialized_candidate_work(
    tmp_path, pg_control_path, category, materialized
):
    path, scope, service, store = _terminal_local_worker_service(
        tmp_path, pg_control_path, category=category, materialized=materialized
    )

    QueueExecutionWorker(service).process_once()

    assert [
        label
        for label in json.loads(path.read_text())["items"][0]["labels"]
        if label.startswith("status:")
    ] == ["status:running"]
    assert not store.events(
        identity("automatic-work-coordinator-auth-recovery", scope.goal_id, "1")
    )


def test_queue_worker_refuses_changed_manifest_artifact_after_original_effect(
    tmp_path, pg_control_path
):
    path, scope, service, store = _terminal_local_worker_service(
        tmp_path,
        pg_control_path,
        category="authentication_required",
        materialized=True,
        provider_artifact="local-file:.drover-native-canary/source-task-changed.json",
        bound_artifact="local-file:.drover-native-canary/source-task-1.json",
    )

    QueueExecutionWorker(service).process_once()

    assert [
        label
        for label in json.loads(path.read_text())["items"][0]["labels"]
        if label.startswith("status:")
    ] == ["status:running"]
    assert not store.events(
        identity("automatic-work-coordinator-auth-recovery", scope.goal_id, "1")
    )


def test_queue_worker_does_not_replay_an_uncertain_recovery_write(
    tmp_path, pg_control_path, monkeypatch
):
    _, scope, service, store = _terminal_local_worker_service(
        tmp_path,
        pg_control_path,
        category="authentication_required",
        materialized=False,
    )
    provider = service.providers[scope.goal_id]

    def fail_set_labels(*args, **kwargs):
        raise OSError("manifest unavailable")

    monkeypatch.setattr(provider, "set_labels", fail_set_labels)
    worker = QueueExecutionWorker(service)
    worker.process_once()
    recovery_key = identity(
        "automatic-work-coordinator-auth-recovery", scope.goal_id, "1"
    )
    assert [
        (event["event_kind"], event["outcome"]) for event in store.events(recovery_key)
    ] == [("intent", None)]
    assert store.links(goal_id=scope.goal_id, number=1)[0]["failure_count"] == 1

    worker.process_once()
    assert len(store.events(recovery_key)) == 1
