import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from drover.server.harness.work_coordinator import parse_work_coordinator_continuity
from drover.server.work_queue.config import GoalScope, WorkQueueConfig
from drover.server.work_queue.github import ProviderUnavailable
from drover.server.work_queue.local_candidates import LocalCandidateProvider
from drover.server.work_queue.provider import ItemRef
from drover.server.work_queue.reconciler import QueueExecutionWorker, ShadowReconciler
from drover.server.work_queue.runtime import HarnessBridge
from drover.server.work_queue.service import (
    WorkActor,
    WorkDenied,
    WorkService,
    identity,
)
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


def test_bridge_terminal_work_done_requires_bound_done_projection(monkeypatch):
    from drover.server.harness.continuity import FactoryObserverContinuity

    bridge = object.__new__(HarnessBridge)
    session = SimpleNamespace(
        session_id="worker-session",
        status="completed",
        handoff_mode="work_coordinator",
        effective_awaiting=None,
    )
    bridge.resolve = lambda run_key: session
    bridge.registry = SimpleNamespace(
        has_successfully_completed_turn=lambda _session_id, _turn_id: True
    )
    bridge.collector = SimpleNamespace(duckdb_path="ignored")
    observed = []

    def status(_self, run_id):
        observed.append(run_id)
        return {"session_id": "worker-session", "worker_state": "done"}

    monkeypatch.setattr(FactoryObserverContinuity, "status", status)
    assert bridge.terminal_work_done("bound-run") is True
    assert observed == ["work-bound-run"]

    monkeypatch.setattr(
        FactoryObserverContinuity,
        "status",
        lambda *_: {"session_id": "worker-session", "worker_state": "blocked"},
    )
    assert bridge.terminal_work_done("bound-run") is False
    monkeypatch.setattr(
        FactoryObserverContinuity,
        "status",
        lambda *_: {"session_id": "other-session", "worker_state": "done"},
    )
    assert bridge.terminal_work_done("bound-run") is False


def test_bridge_terminal_work_done_accepts_only_finished_idle_reusable_turn(
    monkeypatch,
):
    from drover.server.harness.continuity import FactoryObserverContinuity

    bridge = object.__new__(HarnessBridge)
    session = SimpleNamespace(
        session_id="worker-session",
        status="running",
        handoff_mode="work_coordinator",
        effective_awaiting="input",
    )
    bridge.resolve = lambda run_key: session
    bridge.collector = SimpleNamespace(duckdb_path="ignored")
    bridge.registry = SimpleNamespace(
        has_successfully_completed_turn=lambda _session_id, _turn_id: True
    )
    monkeypatch.setattr(
        FactoryObserverContinuity,
        "status",
        lambda *_: {"session_id": "worker-session", "worker_state": "done"},
    )

    assert bridge.terminal_work_done("bound-run") is True

    session.effective_awaiting = None
    assert bridge.terminal_work_done("bound-run") is False
    session.effective_awaiting = "input"
    bridge.registry = SimpleNamespace(
        has_successfully_completed_turn=lambda _session_id, _turn_id: False
    )
    assert bridge.terminal_work_done("bound-run") is False


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

    assert (
        bridge.launch(
            "host",
            "codex",
            "b" * 32,
            ItemRef(
                "local-workspace",
                "local_file",
                "example/project",
                19,
                "local://goal-local/19",
            ),
            "verify",
            None,
            scope.goal_id,
            authority={
                "labels": ["goal:local", "status:review"],
                "body": task,
                "local_candidate_number": 19,
                "local_candidate_cwd": None,
                "expected_effect_artifact": expected,
            },
        )
        == "worker-19"
    )
    assert "separate verification run" in payloads[1][1]["prompt"]
    assert "already committed parent evidence" in payloads[1][1]["prompt"]
    assert "Do not create, replace, or force" in payloads[1][1]["prompt"]


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
                turn_finished=False,
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


@pytest.mark.parametrize(
    ("fixture_name", "numbers"),
    [
        ("native-canary-m2-candidates-26-27.json", [26, 27]),
        ("native-canary-m3-candidates-28-32.json", [28, 29, 30, 31, 32, 33]),
        ("native-canary-m3-candidate-44-verification.json", [44]),
    ],
)
def test_reviewed_native_canary_fixture_manifests_match_local_ingress_schema(
    tmp_path, fixture_name, numbers
):
    """Offline review fixtures remain valid ordinary local-file ingress records."""
    fixture = (
        Path(__file__).parents[1] / "docs" / "operations" / "fixtures" / fixture_name
    )
    path = tmp_path / "candidates.json"
    path.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    scope = GoalScope.model_validate(
        {
            "goal_id": "native-canary-924-848",
            "repo": "arniesaha/drover",
            "goal_label": "goal:native-canary-924-848",
            "candidate_source": "local_file",
            "local_candidates_path": str(path),
            "credential": {
                "token_env": "UNUSED_LOCAL_CANDIDATE_TOKEN",
                "capabilities": ["issues_read"],
            },
        }
    )
    provider = LocalCandidateProvider("native-canary-924-848", scope)

    provider.authenticate()
    items = provider.list_items()

    assert [item["number"] for item in items] == numbers
    assert all("status:queued" in item["operator_authority_labels"] for item in items)
    assert all(
        item["expected_effect_artifact"].startswith("local-file:.drover-native-canary/")
        for item in items
    )
    if fixture_name == "native-canary-m3-candidates-28-32.json":
        integration = [item for item in items if item["number"] in {31, 32}]
        assert all(
            "delivery:integration" in item["operator_authority_labels"]
            for item in integration
        )
        deployment = next(item for item in items if item["number"] == 33)
        assert "delivery:integration" not in deployment["operator_authority_labels"]
    if fixture_name == "native-canary-m3-candidate-44-verification.json":
        body = items[0]["body"]
        assert "git add -f" in body
        assert "Run role is verify" in body
        assert "untracked-only evidence is a verification failure" in body


def test_local_candidate_manifest_rejects_caller_selected_deployment_scope(tmp_path):
    """A local manifest cannot fabricate the deployment human-gate binding."""
    path = tmp_path / "candidates.json"
    _manifest(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["items"][0]["authority_scope"] = "deployment"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    provider = LocalCandidateProvider("local-workspace", _scope(path))
    with pytest.raises(ProviderUnavailable, match="manifest_invalid"):
        provider.authenticate()


def test_policy_opted_local_deployment_selector_is_hub_signed(tmp_path):
    """Only policy/config can select a deployment gate; manifest labels cannot."""
    path = tmp_path / "candidates.json"
    _manifest(path)
    policy_source = (
        Path(__file__).parents[1]
        / "src"
        / "drover"
        / "server"
        / "work_queue"
        / "policy-v1.json"
    )
    policy = json.loads(policy_source.read_text(encoding="utf-8"))
    policy["authority"]["local_candidate_deployment_scope_enabled"] = True
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    scope = _scope(
        path,
        policy_path=str(policy_path),
        execution_enabled=True,
        execution_cwds={"host": str(tmp_path / "checkout")},
        brief_ref="docs/local-canary.md",
        continuity_owner_id="drover:owner/goal-local",
        continuity_owner_harness="codex",
        local_candidate_authority_scopes={"33": "deployment"},
        integration_serialization_key="canary-write",
        integration_serialization_label="delivery:integration",
    )
    config = WorkQueueConfig(
        enabled=True, shadow_mode=False, workspace_id="local-workspace", goals=[scope]
    )
    bridge = object.__new__(HarnessBridge)
    bridge.config = config
    bridge.collector = SimpleNamespace(work_coordinator_continuity_secret="hub-secret")

    binding = bridge._work_coordinator_binding(
        "a" * 32,
        "work",
        "local://goal-local/33",
        scope.goal_id,
        local_candidate_number=33,
    )
    parsed, _ = parse_work_coordinator_continuity(
        {"harness": "codex", "work_coordinator_continuity": binding},
        hub_secret="hub-secret",
    )

    assert parsed.authority_scope == "deployment"
    implementation_binding = bridge._work_coordinator_binding(
        "c" * 32,
        "work",
        "local://goal-local/34",
        scope.goal_id,
        local_candidate_number=34,
    )
    implementation, _ = parse_work_coordinator_continuity(
        {"harness": "codex", "work_coordinator_continuity": implementation_binding},
        hub_secret="hub-secret",
    )
    assert implementation.authority_scope == "implementation"
    with pytest.raises(WorkDenied, match="cannot select integration"):
        bridge._work_coordinator_binding(
            "b" * 32,
            "work",
            "local://goal-local/33",
            scope.goal_id,
            authority_labels=["delivery:integration"],
            local_candidate_number=33,
        )

    policy["authority"]["local_candidate_deployment_scope_enabled"] = False
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(ValueError, match="policy opt-in"):
        _scope(
            path,
            policy_path=str(policy_path),
            execution_enabled=True,
            execution_cwds={"host": str(tmp_path / "checkout")},
            brief_ref="docs/local-canary.md",
            continuity_owner_id="drover:owner/goal-local",
            continuity_owner_harness="codex",
            local_candidate_authority_scopes={"33": "deployment"},
        )


def test_deployment_selector_refuses_recovery_without_existing_signed_session(tmp_path):
    """A lost create response cannot recompute deployment authority from config."""
    path = tmp_path / "candidates.json"
    _manifest(path)
    policy_source = (
        Path(__file__).parents[1]
        / "src"
        / "drover"
        / "server"
        / "work_queue"
        / "policy-v1.json"
    )
    policy = json.loads(policy_source.read_text(encoding="utf-8"))
    policy["authority"]["local_candidate_deployment_scope_enabled"] = True
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    scope = _scope(
        path,
        policy_path=str(policy_path),
        execution_enabled=True,
        execution_cwds={"host": str(tmp_path / "checkout")},
        brief_ref="docs/local-canary.md",
        continuity_owner_id="drover:owner/goal-local",
        continuity_owner_harness="codex",
        local_candidate_authority_scopes={"33": "deployment"},
    )
    bridge = object.__new__(HarnessBridge)
    bridge.config = SimpleNamespace(goals=[scope])
    bridge.collector = SimpleNamespace(
        work_control=SimpleNamespace(
            store=SimpleNamespace(
                links=lambda *, run_key: [
                    {
                        "run_key": run_key,
                        "goal_id": "goal-local",
                        "run_role": "work",
                        "item_number": 33,
                        "item_url": "local://goal-local/33",
                    }
                ]
            ),
            item=lambda *_: (None, None, None, {"operator_authority_labels": []}),
        )
    )

    with pytest.raises(WorkDenied, match="existing signed session"):
        bridge._recovery_payload("a" * 32, {"harness": "codex"})

    # Removing the reviewed selector later cannot downgrade a deployment
    # candidate into implementation authority while reconstructing a lost
    # create response. Local candidate recovery needs an existing signed
    # session regardless of the mutable selector's current value.
    downgraded = scope.model_copy(update={"local_candidate_authority_scopes": {}})
    bridge.config = SimpleNamespace(goals=[downgraded])
    with pytest.raises(WorkDenied, match="existing signed session"):
        bridge._recovery_payload("a" * 32, {"harness": "codex"})


def _deferred_completion_service(
    tmp_path, pg_control_path, *, materialized=True, terminal_done=True
):
    path = tmp_path / "candidates.json"
    expected = "local-file:.drover-native-canary/source-task-28.json"
    _manifest(path, expected_effect=expected)
    scope = _scope(
        path,
        execution_enabled=True,
        execution_cwds={"host": str(tmp_path / "checkout")},
        brief_ref="docs/local-canary.md",
        continuity_owner_id="drover:owner/goal-local",
        continuity_owner_harness="codex",
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
                link
                for link in links
                if not link["session_id"]
                or self.sessions[link["run_key"]].status == "running"
            ]

        def resolve(self, run_key):
            return self.sessions.get(run_key)

        def quota(self):
            return [
                {
                    "provider": "openai",
                    "status": "ok",
                    "hosts": [{"host_id": "host"}],
                    "windows": [{"kind": "primary", "status": "ok", "used_percent": 1}],
                }
            ]

        def eligible(self, goal_id=None, parent_session_id=None):
            return {("host", "codex")}

        def terminal_work_done(self, run_key):
            return terminal_done and self.sessions[run_key].turn_finished

        def expected_effect_materialized(self, run_key, artifact):
            assert artifact == expected
            return materialized

        def coordinator_failure_category(self, run_key):
            return None

        def launch(self, host, harness, run_key, ref, role, parent, goal_id, **kwargs):
            self.launches.append((run_key, role, parent, kwargs))
            self.sessions[run_key] = SimpleNamespace(
                session_id=f"session-{len(self.launches)}",
                status="running",
                host_id=host,
                parent_session_id=parent,
                turn_finished=False,
            )
            return self.sessions[run_key].session_id

    provider = LocalCandidateProvider(config.workspace_id, scope)
    harness = Harness()
    service = WorkService(
        config,
        WorkStore(pg_control_path, config.workspace_id),
        {scope.goal_id: provider},
        harness,
    )
    worker = QueueExecutionWorker(service)
    worker.process_once()
    work = service.store.links(goal_id=scope.goal_id, number=1)[0]
    return scope, provider, harness, service, worker, work


def test_queue_worker_defers_bound_completion_until_terminal_then_launches_verify(
    tmp_path, pg_control_path
):
    scope, provider, harness, service, worker, work = _deferred_completion_service(
        tmp_path, pg_control_path
    )
    actor = WorkActor("credential:coordinator", "nix")

    # The worker records a completion request while its structured turn is
    # still active. Verification cannot launch until that exact work session
    # reaches its terminal boundary.
    assert service.update(
        scope.goal_id,
        1,
        work["run_key"],
        "complete",
        actor=actor,
        estimated_cost=1,
    ) == {"outcome": "deferred"}
    assert len(harness.launches) == 1
    assert service.update(
        scope.goal_id,
        1,
        work["run_key"],
        "complete",
        actor=actor,
        estimated_cost=1,
    ) == {"outcome": "deferred"}
    assert len(service.store.events(identity(work["run_key"], "complete", None))) == 1
    assert [
        label["name"]
        for label in provider.get_item(_ref())["labels"]
        if label["name"].startswith("status:")
    ] == ["status:running"]

    # The ordinary launch surface cannot evade same-item exclusion merely by
    # naming the reusable worker as a parent. Only the internal terminal-proof
    # path below can release that exact parent link.
    service.status(
        provider,
        service.item(scope.goal_id, 1)[1],
        _ref(),
        provider.get_item(_ref()),
        "review",
    )
    request, _ = service._verification_request(
        scope.goal_id, 1, work, provider.get_item(_ref())
    )
    with pytest.raises(WorkDenied, match="existing execution requires inspection"):
        service.launch(
            scope.goal_id,
            1,
            request,
            scope.execution_estimated_cost,
            actor=WorkActor("control:queue-execution", "nix"),
            run_role="verify",
            parent_session_id=work["session_id"],
        )
    service.status(
        provider,
        service.item(scope.goal_id, 1)[1],
        _ref(),
        provider.get_item(_ref()),
        "running",
    )

    # Codex keeps its structured session reusable after a finished turn. The
    # bound terminal proof, not whole-session termination, releases exactly
    # this deferred verification successor.
    harness.sessions[work["run_key"]].status = "running"
    harness.sessions[work["run_key"]].turn_finished = True
    worker.process_once()
    links = service.store.links(goal_id=scope.goal_id, number=1)
    assert [link["run_role"] for link in links] == ["work", "verify"]
    assert harness.launches[-1][1:3] == ("verify", work["session_id"])
    assert [
        label["name"]
        for label in provider.get_item(_ref())["labels"]
        if label["name"].startswith("status:")
    ] == ["status:review"]

    # The durable deferred-completion intent yields one verification successor.
    worker.process_once()
    assert len(service.store.links(goal_id=scope.goal_id, number=1)) == 2


def test_queue_worker_does_not_infer_completion_without_worker_request(
    tmp_path, pg_control_path
):
    """An old terminal worker, such as candidate 28, is never auto-rescued."""
    scope, provider, harness, service, worker, work = _deferred_completion_service(
        tmp_path, pg_control_path
    )
    harness.sessions[work["run_key"]].status = "completed"

    worker.process_once()

    assert [
        link["run_role"]
        for link in service.store.links(goal_id=scope.goal_id, number=1)
    ] == ["work"]
    assert not service.store.events(identity(work["run_key"], "complete", None))
    assert [
        label["name"]
        for label in provider.get_item(_ref())["labels"]
        if label["name"].startswith("status:")
    ] == ["status:running"]


@pytest.mark.parametrize(
    ("terminal", "materialized", "terminal_done"),
    [("errored", True, True), ("completed", False, True), ("completed", True, False)],
    ids=["terminal-error", "missing-bound-effect", "human-or-blocked-terminal"],
)
def test_queue_worker_never_advances_deferred_completion_without_terminal_bound_effect(
    tmp_path, pg_control_path, terminal, materialized, terminal_done
):
    scope, provider, harness, service, worker, work = _deferred_completion_service(
        tmp_path,
        pg_control_path,
        materialized=materialized,
        terminal_done=terminal_done,
    )
    assert service.update(
        scope.goal_id,
        1,
        work["run_key"],
        "complete",
        actor=WorkActor("credential:coordinator", "nix"),
        estimated_cost=1,
    ) == {"outcome": "deferred"}
    harness.sessions[work["run_key"]].status = terminal

    worker.process_once()
    assert [
        link["run_role"]
        for link in service.store.links(goal_id=scope.goal_id, number=1)
    ] == ["work"]
    assert [
        label["name"]
        for label in provider.get_item(_ref())["labels"]
        if label["name"].startswith("status:")
    ] == ["status:running"]
