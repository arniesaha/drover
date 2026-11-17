from types import SimpleNamespace

from drover.server.work_queue.runtime import HarnessBridge


def _bridge(scope, rows):
    bridge = object.__new__(HarnessBridge)
    bridge.config = SimpleNamespace(goals=[scope])
    host = SimpleNamespace(
        host_id="studio",
        capabilities={"harnesses": rows},
        liveness=lambda: SimpleNamespace(usable=True),
    )
    bridge.registry = SimpleNamespace(
        list_hosts=lambda: [host], get_session=lambda _: None
    )
    return bridge


def _row(name, worktree):
    return {
        "name": name,
        "enabled": True,
        "capabilities": {"launch_modes": ["structured"], "worktree": worktree},
    }


def test_native_owner_work_uses_configured_worktree_harness():
    scope = SimpleNamespace(
        goal_id="native",
        execution_cwds={"studio": "/repo"},
        continuity_owner_id="drover:owner/native",
        continuity_owner_harness="codex",
    )
    bridge = _bridge(scope, [_row("claude-code", False), _row("codex", True)])
    assert bridge.eligible("native") == {("studio", "codex")}


def test_native_owner_work_fails_closed_without_configured_worktree_harness():
    scope = SimpleNamespace(
        goal_id="native",
        execution_cwds={"studio": "/repo"},
        continuity_owner_id="drover:owner/native",
        continuity_owner_harness="codex",
    )
    bridge = _bridge(scope, [_row("claude-code", False), _row("codex", False)])
    assert bridge.eligible("native") == set()


def test_native_owner_work_fails_closed_without_configured_harness():
    scope = SimpleNamespace(
        goal_id="native",
        execution_cwds={"studio": "/repo"},
        continuity_owner_id="drover:owner/native",
        continuity_owner_harness=None,
    )
    bridge = _bridge(scope, [_row("claude-code", False), _row("codex", True)])
    assert bridge.eligible("native") == set()


def test_native_owner_work_ignores_malformed_capabilities():
    scope = SimpleNamespace(
        goal_id="native",
        execution_cwds={"studio": "/repo"},
        continuity_owner_id="drover:owner/native",
        continuity_owner_harness="codex",
    )
    bridge = _bridge(scope, [{"name": "codex", "enabled": True, "capabilities": None}])
    assert bridge.eligible("native") == set()


def test_non_native_owner_goal_keeps_existing_eligible_harnesses():
    scope = SimpleNamespace(
        goal_id="ordinary",
        execution_cwds={"studio": "/repo"},
        continuity_owner_id="agent:coder:main",
        continuity_owner_harness=None,
    )
    bridge = _bridge(scope, [_row("claude-code", False), _row("codex", True)])
    assert bridge.eligible("ordinary") == {
        ("studio", "claude-code"),
        ("studio", "codex"),
    }
