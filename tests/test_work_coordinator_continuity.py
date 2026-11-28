import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from drover.schema import bootstrap_control_plane_store
from drover.server.harness.continuity import (
    ContinuityConflict,
    FactoryObserverContinuity,
    record_work_coordinator_state,
)
from drover.server.harness.factory_observer import FactoryObserverRequestError
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.work_coordinator import (
    parse_unverified_work_coordinator_continuity,
    parse_work_coordinator_continuity,
    sign_work_coordinator_continuity,
)


def test_bridge_recovery_reconstructs_signed_continuity_and_initializes_ledger(
    tmp_path,
):
    from drover.server.metrics import MetricsCollector
    from drover.server.work_queue.runtime import HarnessBridge

    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    run_key = "recovery-key"
    remote = {
        "session_id": "harness-recovery",
        "client_session_id": run_key,
        "harness": "codex",
        "status": "running",
    }
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        api_token="hub-secret",
        work_coordinator_continuity_secret="hub-continuity-secret",
    )
    collector.work_control = SimpleNamespace(
        store=SimpleNamespace(
            links=lambda *, run_key: [
                {
                    "run_key": run_key,
                    "goal_id": "goal",
                    "run_role": "work",
                    "item_url": "https://github.com/example/project/issues/804",
                }
            ]
        ),
        register_hub_launch=lambda *args, **kwargs: None,
    )
    collector._harness_request = lambda *args, **kwargs: (
        200,
        json.dumps({"sessions": [remote]}),
    )
    registry = HarnessRegistry(path)
    bridge = object.__new__(HarnessBridge)
    bridge.collector = collector
    bridge.config = SimpleNamespace(
        goals=[
            SimpleNamespace(
                goal_id="goal", continuity_owner_id="agent:main:telegram:owner"
            )
        ]
    )
    bridge.registry = SimpleNamespace(
        session_by_client_id=registry.session_by_client_id,
        list_hosts=lambda: [
            SimpleNamespace(
                host_id="studio", liveness=lambda: SimpleNamespace(usable=True)
            )
        ],
    )

    session = bridge.resolve(run_key)

    assert session.session_id == "harness-recovery"
    status = FactoryObserverContinuity(path).status("work-" + run_key)
    assert [event["report"]["kind"] for event in status["events"]] == ["worker_started"]


def test_github_native_owner_wake_uses_the_signed_parent_factory_run(tmp_path):
    """A replay cannot create a second owner wake from a coordinator outcome."""
    from drover.server.metrics import MetricsCollector
    from drover.server.work_queue.runtime import HarnessBridge

    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    registry = HarnessRegistry(path)
    session = registry.create_session(
        session_id="github-parent",
        host_id="studio",
        harness="codex",
        command="codex",
        status="running",
        handoff_mode="work_coordinator",
        source_session_id="work/work-github-parent@0",
    )
    store = FactoryObserverContinuity(path)
    run_id = store.initialize(
        session_id=session.session_id,
        objective="Exact GitHub work",
        checkpoint="launched",
        authorized_owner_id="drover:owner/goal",
    )
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        api_token="hub-secret",
    )
    bridge = object.__new__(HarnessBridge)
    bridge.collector = collector
    bridge.resolve = lambda key: session if key == "github-parent" else None

    first = bridge.github_native_owner_wake(
        "github-parent",
        source_event_id="a" * 64,
        subject="https://github.com/example/project/issues/1",
        summary="provider facts digest",
    )
    second = bridge.github_native_owner_wake(
        "github-parent",
        source_event_id="a" * 64,
        subject="https://github.com/example/project/issues/1",
        summary="provider facts digest",
    )

    assert first == second
    status = store.status(run_id)
    assert [event["report"]["kind"] for event in status["events"]] == [
        "github_acceptance_verified"
    ]


def test_work_coordinator_binding_is_exact_and_projects_start_done_blocked_and_approval(
    tmp_path,
):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    unsigned = {
        "run_id": "work-abc123",
        "owner_id": "agent:main:telegram:owner",
        "authority_scope": "implementation",
        "objective": "Issue #804 work",
    }
    binding, body = parse_work_coordinator_continuity(
        {
            "harness": "codex",
            "work_coordinator_continuity": {
                **unsigned,
                "binding_signature": sign_work_coordinator_continuity(unsigned, "hub"),
            },
        },
        hub_secret="hub",
    )
    assert binding and "work_coordinator_continuity" not in body
    registry = HarnessRegistry(path)
    session = registry.create_session(
        session_id="harness-work",
        host_id="studio",
        harness="codex",
        command="codex",
        status="running",
        handoff_mode="work_coordinator",
        source_session_id=binding.source_session_id,
    )
    store = FactoryObserverContinuity(path)
    run = store.initialize(
        session_id=session.session_id,
        objective=binding.objective,
        checkpoint="launched",
        authority_scope=binding.authority_scope,
        authorized_owner_id=binding.owner_id,
    )
    store.report(
        run,
        source="coordinator",
        source_event_id="launch:harness-work",
        subject="harness-work",
        sequence=0,
        kind="worker_started",
        summary="started",
    )
    with store._transaction(run) as (con, _):
        for seq, state in enumerate(
            (
                "Drover-State: blocked reason=missing-check",
                "Drover-State: needs-approval gate=merge reason=ready",
                "Drover-State: done sha=" + "a" * 40,
            ),
            1,
        ):
            assert record_work_coordinator_state(
                con,
                event_id=f"event-{seq}",
                session_id="harness-work",
                event_type="assistant_output",
                sequence=seq,
                payload={"text": state},
                received_at=datetime.now(timezone.utc),
            )
    reports = [event["report"]["kind"] for event in store.status(run)["events"]]
    assert reports == [
        "worker_started",
        "worker_blocked",
        "worker_needs_approval",
        "worker_done",
    ]
    assert store.status(run)["owner_handoff"]["authorized_owner_id"] == binding.owner_id


def test_signed_candidate_effect_binding_is_persisted_and_rejects_generic_receipts(
    tmp_path,
):
    """The hub persists the signed local effect before any owner delivery."""
    from drover.server.metrics import MetricsCollector

    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    expected = "local-file:.drover-native-canary/source-task-9.json"
    unsigned = {
        "run_id": "work-expected-effect",
        "owner_id": "drover:owner/native-canary",
        "authority_scope": "implementation",
        "objective": "Bounded candidate effect",
        "expected_effect_artifact": expected,
    }
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        api_token="hub-secret",
        work_coordinator_continuity_secret="hub-continuity-secret",
    )
    MetricsCollector._sync_created_harness_session(
        collector,
        "studio",
        {
            "harness": "codex",
            "work_coordinator_continuity": {
                **unsigned,
                "binding_signature": sign_work_coordinator_continuity(
                    unsigned, "hub-continuity-secret"
                ),
            },
        },
        json.dumps({"session_id": "harness-expected-effect", "status": "running"}),
    )
    store = FactoryObserverContinuity(path)
    status = store.status("work-expected-effect")
    assert status["expected_effect_artifact"] == expected
    assert (
        HarnessRegistry(path).work_coordinator_expected_effect_artifact(
            "harness-expected-effect", "work-expected-effect"
        )
        == expected
    )
    assert (
        HarnessRegistry(path).work_coordinator_expected_effect_artifact(
            "harness-expected-effect", "work-other"
        )
        is None
    )
    lease = store.lease("work-expected-effect", owner_id=unsigned["owner_id"])
    start = store.consume(
        "work-expected-effect",
        owner_id=unsigned["owner_id"],
        owner_epoch=lease["owner_epoch"],
    )
    # Starting a candidate is only an observation. It must not require or
    # consume the final candidate effect before the worker has finished.
    assert start["type"] == "observe_worker_start"
    assert "expected_effect_artifact" not in start
    store.action_intent(
        "work-expected-effect",
        owner_id=unsigned["owner_id"],
        owner_epoch=lease["owner_epoch"],
        wake_id=start["event_id"],
        action_fingerprint=start["action_id"],
        artifact="local-file:.drover-native-canary/actions/start-observed.json",
    )
    store.acknowledge(
        "work-expected-effect",
        owner_id=unsigned["owner_id"],
        owner_epoch=lease["owner_epoch"],
        event_id=start["event_id"],
        checkpoint="worker start observed",
    )

    done = store.report(
        "work-expected-effect",
        source="drover-work-coordinator",
        source_event_id="harness-expected-effect:done",
        subject="harness-expected-effect",
        sequence=1,
        kind="worker_done",
        summary="Bounded candidate effect completed.",
    )
    action = store.consume(
        "work-expected-effect",
        owner_id=unsigned["owner_id"],
        owner_epoch=lease["owner_epoch"],
    )
    assert action["type"] == "review_worker_result"
    assert action["expected_effect_artifact"] == expected
    with pytest.raises(ContinuityConflict, match="expected effect"):
        store.action_intent(
            "work-expected-effect",
            owner_id=unsigned["owner_id"],
            owner_epoch=lease["owner_epoch"],
            wake_id=done,
            action_fingerprint=action["action_id"],
            artifact="local-file:.drover-native-canary/actions/generic-wake.json",
        )
    terminal_receipt = store.action_intent(
        "work-expected-effect",
        owner_id=unsigned["owner_id"],
        owner_epoch=lease["owner_epoch"],
        wake_id=done,
        action_fingerprint=action["action_id"],
        artifact=expected,
    )
    assert terminal_receipt["state"] == "complete"
    assert terminal_receipt["artifact"] == expected
    store.acknowledge(
        "work-expected-effect",
        owner_id=unsigned["owner_id"],
        owner_epoch=lease["owner_epoch"],
        event_id=done,
        checkpoint="candidate effect verified",
    )


def test_work_coordinator_binding_rejects_noncanonical_owner():
    try:
        unsigned = {
            "run_id": "work-abc123",
            "owner_id": "operator",
            "authority_scope": "implementation",
            "objective": "x",
        }
        parse_work_coordinator_continuity(
            {
                "work_coordinator_continuity": {
                    **unsigned,
                    "binding_signature": sign_work_coordinator_continuity(
                        unsigned, "hub"
                    ),
                }
            },
            hub_secret="hub",
        )
    except ValueError as exc:
        assert "canonical" in str(exc)
    else:
        raise AssertionError("noncanonical owner accepted")


def test_integration_serialization_binding_is_signed_and_retains_commit_only_scope():
    """A serialization class is metadata, never the integration authority."""
    unsigned = {
        "run_id": "work-integration-abc",
        "owner_id": "drover:owner/goal",
        "authority_scope": "implementation",
        "objective": "Serialized implementation work",
        "delivery_role": "integration_serialized",
        "repository": "example/project",
        "serialization_key": "main-write",
    }

    binding, _ = parse_work_coordinator_continuity(
        {
            "work_coordinator_continuity": {
                **unsigned,
                "binding_signature": sign_work_coordinator_continuity(unsigned, "hub"),
            }
        },
        hub_secret="hub",
    )

    assert binding is not None
    assert binding.authority_scope == "implementation"
    assert binding.delivery_role == "integration_serialized"
    assert binding.repository == "example/project"
    assert binding.serialization_key == "main-write"


def test_integration_serialization_binding_rejects_authority_escalation():
    unsigned = {
        "run_id": "work-integration-abc",
        "owner_id": "drover:owner/goal",
        "authority_scope": "integration",
        "objective": "Serialized implementation work",
        "delivery_role": "integration_serialized",
        "repository": "example/project",
        "serialization_key": "main-write",
    }

    with pytest.raises(ValueError, match="implementation"):
        parse_work_coordinator_continuity(
            {
                "work_coordinator_continuity": {
                    **unsigned,
                    "binding_signature": sign_work_coordinator_continuity(
                        unsigned, "hub"
                    ),
                }
            },
            hub_secret="hub",
        )


def test_work_coordinator_committed_provenance_requires_signed_item_binding():
    unsigned = {
        "run_id": "work-committed-provenance",
        "owner_id": "drover:owner/goal",
        "authority_scope": "implementation",
        "objective": "Committed local provenance",
        "committed_provenance": True,
    }
    with pytest.raises(ValueError, match="requires item binding"):
        parse_work_coordinator_continuity(
            {
                "work_coordinator_continuity": {
                    **unsigned,
                    "binding_signature": sign_work_coordinator_continuity(
                        unsigned, "hub"
                    ),
                }
            },
            hub_secret="hub",
        )

    signed = {**unsigned, "goal_id": "goal", "item_number": 48}
    with pytest.raises(ValueError, match="requires expected effect"):
        parse_work_coordinator_continuity(
            {
                "work_coordinator_continuity": {
                    **signed,
                    "binding_signature": sign_work_coordinator_continuity(
                        signed, "hub"
                    ),
                }
            },
            hub_secret="hub",
        )
    signed["expected_effect_artifact"] = (
        "local-file:.drover-native-canary/source-task-48.json"
    )
    binding, _ = parse_work_coordinator_continuity(
        {
            "work_coordinator_continuity": {
                **signed,
                "binding_signature": sign_work_coordinator_continuity(signed, "hub"),
            }
        },
        hub_secret="hub",
    )
    assert binding is not None and binding.committed_provenance is True


def test_work_coordinator_binding_rejects_forged_hub_signature():
    unsigned = {
        "run_id": "work-abc123",
        "owner_id": "agent:main:telegram:owner",
        "authority_scope": "implementation",
        "objective": "Issue #804 work",
    }
    with pytest.raises(ValueError, match="hub authenticated"):
        parse_work_coordinator_continuity(
            {
                "work_coordinator_continuity": {
                    **unsigned,
                    "binding_signature": "forged",
                }
            },
            hub_secret="hub",
        )


def test_hub_only_binding_secret_is_not_a_harness_bearer_token():
    unsigned = {
        "run_id": "work-abc123",
        "owner_id": "agent:main:telegram:owner",
        "authority_scope": "implementation",
        "objective": "Issue #804 work",
    }
    body = {
        "work_coordinator_continuity": {
            **unsigned,
            "binding_signature": sign_work_coordinator_continuity(
                unsigned, "hub-continuity-secret"
            ),
        }
    }
    with pytest.raises(ValueError, match="hub authenticated"):
        parse_work_coordinator_continuity(body, hub_secret="harness-bearer-token")
    binding, _ = parse_work_coordinator_continuity(
        body, hub_secret="hub-continuity-secret"
    )
    assert binding is not None
    host_binding, _ = parse_unverified_work_coordinator_continuity(body)
    assert host_binding == binding


def test_bridge_quota_refreshes_through_collector_transport(monkeypatch, tmp_path):
    from drover.server.work_queue.runtime import HarnessBridge

    collector = SimpleNamespace(
        duckdb_path=tmp_path / "control.duckdb",
        fetch_harness_provider_usage=object(),
    )
    bridge = object.__new__(HarnessBridge)
    bridge.collector = collector
    calls = []

    def quota(**kwargs):
        calls.append(kwargs)
        return {"accounts": [{"provider": "openai"}]}

    monkeypatch.setattr("drover.server.providers.quota_read.read_provider_quota", quota)

    assert bridge.quota() == [{"provider": "openai"}]
    assert calls == [
        {
            "duckdb_path": collector.duckdb_path,
            "fresh": True,
            "fetch": collector.fetch_harness_provider_usage,
        }
    ]


def test_hub_relay_replay_projects_coordinator_state_once(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    registry = HarnessRegistry(path)
    registry.create_session(
        session_id="harness-relay",
        host_id="studio",
        harness="codex",
        command="codex",
        status="running",
        handoff_mode="work_coordinator",
        source_session_id="work/work-relay@0",
    )
    store = FactoryObserverContinuity(path)
    run = store.initialize(
        session_id="harness-relay",
        objective="Relay state",
        checkpoint="launched",
        authorized_owner_id="agent:main:telegram:owner",
    )
    record = {
        "event_id": "relay-state-1",
        "session_id": "harness-relay",
        "event_type": "assistant_output",
        "seq": 1,
        "payload": {"text": "Drover-State: done sha=" + "b" * 40},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    assert registry.ingest_structured_events([record]) == 1
    assert registry.ingest_structured_events([record]) == 0
    reports = [event["report"]["kind"] for event in store.status(run)["events"]]
    assert reports == ["worker_done"]


def test_hub_sync_is_the_only_ledger_initializer(tmp_path):
    import json

    from drover.server.metrics import MetricsCollector

    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        api_token="hub-secret",
        work_coordinator_continuity_secret="hub-continuity-secret",
    )
    unsigned = {
        "run_id": "work-hub-init",
        "owner_id": "agent:main:telegram:owner",
        "authority_scope": "implementation",
        "objective": "Hub owns this ledger",
    }
    payload = {
        "harness": "codex",
        "mode": "structured",
        "work_coordinator_continuity": {
            **unsigned,
            "binding_signature": sign_work_coordinator_continuity(
                unsigned, "hub-continuity-secret"
            ),
        },
    }
    MetricsCollector._sync_created_harness_session(
        collector,
        "studio",
        payload,
        json.dumps({"session_id": "harness-hub", "status": "running"}),
    )
    session = HarnessRegistry(path).get_session("harness-hub")
    assert session.handoff_mode == "work_coordinator"
    assert session.source_session_id == "work/work-hub-init@0"
    status = FactoryObserverContinuity(path).status("work-hub-init")
    assert [event["report"]["kind"] for event in status["events"]] == ["worker_started"]


def test_registry_preserves_signed_committed_provenance_tristate(tmp_path):
    """Only an initialized binding can distinguish false from legacy unknown."""
    from drover.server.db import control_plane_connection

    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    registry = HarnessRegistry(path)
    store = FactoryObserverContinuity(path)
    runs = {}
    for name, provenance in (("true", True), ("false", False), ("legacy", False)):
        session = registry.create_session(
            session_id=f"harness-provenance-{name}",
            host_id="studio",
            harness="codex",
            command="codex",
            status="running",
            handoff_mode="work_coordinator",
            source_session_id=f"work/work-provenance-{name}@0",
        )
        runs[name] = store.initialize(
            session_id=session.session_id,
            objective=f"Provenance {name}",
            checkpoint="launched",
            expected_effect_artifact="local-file:.drover-native-canary/effect.json",
            committed_provenance=provenance,
        )
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE factory_observer_runs SET committed_provenance = NULL WHERE run_id = ?",
            [runs["legacy"]],
        )

    assert (
        registry.work_coordinator_committed_provenance(
            "harness-provenance-true", runs["true"]
        )
        is True
    )
    assert (
        registry.work_coordinator_committed_provenance(
            "harness-provenance-false", runs["false"]
        )
        is False
    )
    assert (
        registry.work_coordinator_committed_provenance(
            "harness-provenance-legacy", runs["legacy"]
        )
        is None
    )
    assert store.status(runs["legacy"])["committed_provenance"] is None


def test_hub_sync_persists_immutable_committed_provenance_mode(tmp_path):
    """Only the hub-signed binding may select a verifier-capable work run."""
    import json

    from drover.server.metrics import MetricsCollector

    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        api_token="hub-secret",
        work_coordinator_continuity_secret="hub-continuity-secret",
    )
    unsigned = {
        "run_id": "work-hub-committed",
        "owner_id": "agent:main:telegram:owner",
        "authority_scope": "implementation",
        "objective": "Bound committed provenance",
        "goal_id": "goal",
        "item_number": 48,
        "expected_effect_artifact": "local-file:.drover-native-canary/source-task-48.json",
        "committed_provenance": True,
    }
    payload = {
        "harness": "codex",
        "mode": "structured",
        "work_coordinator_continuity": {
            **unsigned,
            "binding_signature": sign_work_coordinator_continuity(
                unsigned, "hub-continuity-secret"
            ),
        },
    }
    response = json.dumps({"session_id": "harness-hub-committed", "status": "running"})
    MetricsCollector._sync_created_harness_session(
        collector, "studio", payload, response
    )
    status = FactoryObserverContinuity(path).status("work-hub-committed")
    assert status["committed_provenance"] is True

    unsigned["committed_provenance"] = False
    payload["work_coordinator_continuity"] = {
        **unsigned,
        "binding_signature": sign_work_coordinator_continuity(
            unsigned, "hub-continuity-secret"
        ),
    }
    with pytest.raises(RuntimeError, match="hub continuity initialization failed"):
        MetricsCollector._sync_created_harness_session(
            collector, "studio", payload, response
        )
    assert (
        FactoryObserverContinuity(path).status("work-hub-committed")[
            "committed_provenance"
        ]
        is True
    )


def test_hub_sync_fails_closed_when_binding_cannot_initialize(tmp_path):
    import json

    from drover.server.metrics import MetricsCollector

    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    HarnessRegistry(path).create_session(
        session_id="harness-conflict",
        host_id="studio",
        harness="codex",
        command="codex",
        source_session_id="work/other-run@0",
        handoff_mode="work_coordinator",
    )
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        api_token="hub-secret",
        work_coordinator_continuity_secret="hub-continuity-secret",
    )
    unsigned = {
        "run_id": "work-hub-conflict",
        "owner_id": "agent:main:telegram:owner",
        "authority_scope": "implementation",
        "objective": "Must not be rebound",
    }
    with pytest.raises(RuntimeError, match="hub continuity initialization failed"):
        MetricsCollector._sync_created_harness_session(
            collector,
            "studio",
            {
                "harness": "codex",
                "work_coordinator_continuity": {
                    **unsigned,
                    "binding_signature": sign_work_coordinator_continuity(
                        unsigned, "hub-continuity-secret"
                    ),
                },
            },
            json.dumps({"session_id": "harness-conflict", "status": "running"}),
        )
    with pytest.raises(KeyError):
        FactoryObserverContinuity(path).status("work-hub-conflict")


def test_hub_sync_rejects_success_response_without_session_id(tmp_path):
    from drover.server.metrics import MetricsCollector

    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
    )
    with pytest.raises(ValueError, match="lacks session_id"):
        collector._sync_created_harness_session("studio", {}, '{"status": "running"}')
    with pytest.raises(ValueError, match="valid JSON"):
        collector._sync_created_harness_session("studio", {}, "not-json")


def test_work_continuity_item_binding_is_signed_and_requires_the_complete_pair():
    unsigned = {
        "run_id": "work-" + "d" * 32,
        "owner_id": "drover:owner/native",
        "authority_scope": "implementation",
        "objective": "bound worker update",
        "goal_id": "native-canary-924-848",
        "item_number": 41,
    }
    binding, _ = parse_work_coordinator_continuity(
        {
            "work_coordinator_continuity": {
                **unsigned,
                "binding_signature": sign_work_coordinator_continuity(unsigned, "hub"),
            }
        },
        hub_secret="hub",
    )
    assert binding is not None
    assert (binding.goal_id, binding.item_number) == ("native-canary-924-848", 41)

    partial = dict(unsigned)
    partial.pop("item_number")
    with pytest.raises(FactoryObserverRequestError, match="item binding"):
        parse_unverified_work_coordinator_continuity(
            {"work_coordinator_continuity": {**partial, "binding_signature": "x"}}
        )


def test_signed_m2_handoff_requires_bound_effect_and_cannot_be_body_selected():
    unsigned = {
        "run_id": "work-m2-handoff",
        "owner_id": "drover:owner/goal",
        "authority_scope": "implementation",
        "objective": "M2 receipt handoff",
        "m2_ack_pending": True,
    }
    with pytest.raises(ValueError, match="requires item binding"):
        parse_work_coordinator_continuity(
            {
                "work_coordinator_continuity": {
                    **unsigned,
                    "binding_signature": sign_work_coordinator_continuity(
                        unsigned, "hub"
                    ),
                }
            },
            hub_secret="hub",
        )

    signed = {
        **unsigned,
        "goal_id": "goal",
        "item_number": 51,
        "expected_effect_artifact": "local-file:.drover-native-canary/m2-51.json",
    }
    binding, _ = parse_work_coordinator_continuity(
        {
            "work_coordinator_continuity": {
                **signed,
                "binding_signature": sign_work_coordinator_continuity(signed, "hub"),
            }
        },
        hub_secret="hub",
    )
    assert binding is not None and binding.m2_ack_pending is True

    forged = {**signed, "m2_ack_pending": False}
    with pytest.raises(ValueError, match="hub authenticated"):
        parse_work_coordinator_continuity(
            {
                "work_coordinator_continuity": {
                    **forged,
                    "binding_signature": (
                        signed["binding_signature"]
                        if "binding_signature" in signed
                        else "forged"
                    ),
                }
            },
            hub_secret="hub",
        )
