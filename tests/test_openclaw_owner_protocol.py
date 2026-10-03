"""Mock normal tool invocation -> real durable boundary, not OpenClaw runtime.

The mock endpoint is the existing continuity_request contract over a real
DuckDB control store. PostgreSQL behavior is proved in its separate module.
No OpenClaw messaging, RPC, runtime registration, or shell invocation occurs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from drover.schema import bootstrap_control_plane_store
from drover.server.harness.continuity import (
    ContinuityConflict,
    FactoryObserverContinuity,
    continuity_request,
)
from drover.server.harness.openclaw_owner import (
    ENDPOINT,
    TOOL_NAME,
    OpenClawOwnerAdapter,
    owner_tool_definition,
)
from drover.server.harness.registry import HarnessRegistry

RUN = "run_OPENCLAW497"
OWNER = "agent:coder:subagent:mock-parent"


class Clock:
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class MockEndpoint:
    """Mock transport reconstructs the durable store for every endpoint call."""

    def __init__(self, path, clock):
        self.path, self.clock = path, clock
        self.calls = []
        self.lose_consume_reply = False

    def store(self):
        return FactoryObserverContinuity(self.path, clock=self.clock)

    def get(self, path, *, query):
        assert path == ENDPOINT
        self.calls.append(("GET", query))
        return {"continuity": self.store().status(**query)}

    def post(self, path, *, body):
        assert path == ENDPOINT
        self.calls.append(("POST", body))
        reply = continuity_request(self.store(), body)
        if body["operation"] == "consume" and self.lose_consume_reply:
            self.lose_consume_reply = False
            raise ConnectionError("Mock transport lost reply after durable consume")
        return reply


class MockNormalToolHarness:
    """A mock parent explicitly invokes a registered tool by name and JSON args.

    This is an in-process integration harness, not a real SDK registration.
    It executes only the adapter boundary; there is no owner-action executor.
    """

    def __init__(self, adapter):
        self.definition = owner_tool_definition()
        self.adapter = adapter

    def invoke_tool(self, name, args):
        assert name == self.definition["name"]
        decoded = json.loads(json.dumps(args))
        return self.adapter.invoke(decoded).model_dump(mode="json")

    def call(self, operation, **fields):
        return self.invoke_tool(
            TOOL_NAME, {"version": 1, "request": {"operation": operation, **fields}}
        )


@pytest.fixture
def make_harness(tmp_path):
    def make(scope="implementation"):
        path = tmp_path / f"{scope}.duckdb"
        bootstrap_control_plane_store(path)
        registry = HarnessRegistry(path)
        registry.create_session(
            session_id="mock-observer",
            host_id="studio",
            harness="codex",
            command="codex",
            handoff_mode="factory_observer",
            source_session_id=f"factory/{RUN}@4",
        )
        clock = Clock()
        endpoint = MockEndpoint(path, clock)
        endpoint.store().initialize(
            session_id="mock-observer",
            objective="Protocol proof",
            checkpoint="Waiting for owner review",
            authority_scope=scope,
        )
        adapter = OpenClawOwnerAdapter(
            endpoint, run_id=RUN, owner_id=OWNER, authority_scope=scope
        )
        return MockNormalToolHarness(adapter), endpoint, clock

    return make


def worker_report(kind="worker_brief_completed"):
    return dict(
        source_event_id="worker:mock:report:1",
        subject="worker:mock",
        sequence=1,
        kind=kind,
        summary="Report only; commit ready for review",
    )


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("worker_brief_completed", "review_worker_result"),
        ("ci_green", "review_ci"),
        ("ci_red", "correct_ci"),
    ],
)
def test_report_owner_action_ack_restart_has_no_duplicate_delivery(
    make_harness, kind, expected
):
    harness, endpoint, _ = make_harness()
    epoch = harness.call("lease")["continuity"]["owner"]["epoch"]
    report = worker_report(kind)
    event_id = harness.call("report", **report)["event_id"]
    assert endpoint.store().status(RUN)["inbox_counts"] == {"pending": 1}
    assert harness.call("poll")["continuity"]["owner_wake"]
    delivery = harness.call("consume", owner_epoch=epoch)["delivery"]
    assert delivery["type"] == expected and delivery["authorized"]
    assert delivery["event_id"] == event_id
    assert delivery["authority_scope"] == "implementation"
    assert harness.call("poll")["continuity"]["next_action"] == delivery
    # Receipt alone is not ack. The mock parent explicitly reconciles and acks.
    harness.call(
        "acknowledge",
        owner_epoch=epoch,
        event_id=event_id,
        checkpoint="Parent reviewed commit; no release inferred",
    )
    reconstructed = MockNormalToolHarness(
        OpenClawOwnerAdapter(
            MockEndpoint(endpoint.path, endpoint.clock),
            run_id=RUN,
            owner_id=OWNER,
            authority_scope="implementation",
        )
    )
    status = reconstructed.call("poll")["continuity"]
    assert status["checkpoint"] == "Parent reviewed commit; no release inferred"
    assert status["inbox_counts"] == {"acknowledged": 1}
    assert not status["terminal_release"]
    assert reconstructed.call("report", **report)["event_id"] == event_id
    assert reconstructed.call("consume", owner_epoch=epoch)["delivery"] is None


def test_lost_delivery_retains_action_and_recovery_is_explicit(make_harness):
    harness, endpoint, clock = make_harness()
    epoch = harness.call("lease")["continuity"]["owner"]["epoch"]
    event_id = harness.call("report", **worker_report())["event_id"]
    endpoint.lose_consume_reply = True
    with pytest.raises(ConnectionError, match="lost reply"):
        harness.call("consume", owner_epoch=epoch)
    assert [
        body["operation"] for method, body in endpoint.calls if method == "POST"
    ] == ["lease", "report", "consume"]
    assert endpoint.store().status(RUN)["inbox_counts"] == {"delivered": 1}
    recovered = MockNormalToolHarness(
        OpenClawOwnerAdapter(
            MockEndpoint(endpoint.path, clock),
            run_id=RUN,
            owner_id=OWNER,
            authority_scope="implementation",
        )
    )
    action = recovered.call("poll")["continuity"]["next_action"]
    assert action["event_id"] == event_id
    assert recovered.call("consume", owner_epoch=epoch)["delivery"] is None
    clock.advance(5)
    assert recovered.call("consume", owner_epoch=epoch)["delivery"] == action
    # A real owner must reconcile external effects idempotently on action_id.
    recovered.call(
        "acknowledge",
        owner_epoch=epoch,
        event_id=event_id,
        checkpoint="Owner reconciled action identity after transport failure",
    )
    assert endpoint.store().status(RUN)["next_action"] is None


@pytest.mark.parametrize(
    "scope,expected,authorized",
    [
        ("integration", "review_ci", True),
        ("deployment", "request_explicit_approval", False),
    ],
)
def test_trusted_scope_is_preserved_without_automatic_release(
    make_harness, scope, expected, authorized
):
    harness, endpoint, _ = make_harness(scope)
    epoch = harness.call("lease")["continuity"]["owner"]["epoch"]
    event_id = harness.call("report", **worker_report("ci_green"))["event_id"]
    action = harness.call("consume", owner_epoch=epoch)["delivery"]
    assert action["type"] == expected and action["authorized"] == authorized
    assert action["authority_scope"] == scope
    assert endpoint.store().status(RUN)["terminal_release"] is False
    if scope == "deployment":
        with pytest.raises(ContinuityConflict, match="approval"):
            harness.call(
                "acknowledge",
                owner_epoch=epoch,
                event_id=event_id,
                checkpoint="Claimed deployment",
            )
        assert endpoint.store().status(RUN)["next_action"] == action
        assert not harness.call("poll")["continuity"]["owner_wake"]


@pytest.mark.parametrize(
    "call_fields",
    [
        {"operation": "merge"},
        {"operation": "deploy"},
        {"operation": "lease", "owner_id": "spoofed-owner"},
        {"operation": "consume", "owner_epoch": "1"},
        {"operation": "consume", "owner_epoch": True},
        {"operation": "consume", "owner_epoch": 1, "authority_scope": "deployment"},
        {"operation": "poll", "run_id": "run_OTHER"},
        {
            "operation": "acknowledge",
            "owner_epoch": 1,
            "event_id": "a" * 64,
            "checkpoint": "Reviewed",
            "approve": True,
        },
    ],
)
def test_invalid_or_authority_broadening_calls_never_reach_transport(
    make_harness, call_fields
):
    harness, endpoint, _ = make_harness()
    with pytest.raises(ValidationError):
        harness.invoke_tool(TOOL_NAME, {"version": 1, "request": call_fields})
    assert endpoint.calls == []


def test_invalid_version_and_untrusted_response_fail_closed(make_harness):
    harness, endpoint, _ = make_harness()
    with pytest.raises(ValidationError):
        harness.invoke_tool(TOOL_NAME, {"version": 2, "request": {"operation": "poll"}})
    epoch = harness.call("lease")["continuity"]["owner"]["epoch"]
    harness.call("report", **worker_report())
    original = endpoint.post

    def altered(path, *, body):
        reply = original(path, body=body)
        reply["delivery"]["type"] = "merge"
        return reply

    endpoint.post = altered
    with pytest.raises(ValidationError):
        harness.call("consume", owner_epoch=epoch)
    status = endpoint.store().status(RUN)
    assert status["next_action"]["type"] == "review_worker_result"
    assert status["inbox_counts"] == {"delivered": 1}
    assert all(
        body.get("operation") != "acknowledge"
        for method, body in endpoint.calls
        if method == "POST"
    )


def test_wrong_trusted_scope_is_refused_before_mutating_the_store(make_harness):
    harness, endpoint, _ = make_harness()
    epoch = harness.call("lease")["continuity"]["owner"]["epoch"]
    event_id = harness.call("report", **worker_report())["event_id"]
    harness.call("consume", owner_epoch=epoch)
    endpoint.calls.clear()
    wrongly_bound = OpenClawOwnerAdapter(
        endpoint, run_id=RUN, owner_id=OWNER, authority_scope="integration"
    )
    with pytest.raises(ValueError, match="trusted owner run/scope"):
        wrongly_bound.invoke(
            {
                "version": 1,
                "request": {
                    "operation": "acknowledge",
                    "owner_epoch": epoch,
                    "event_id": event_id,
                    "checkpoint": "Wrong authority",
                },
            }
        )
    assert [method for method, _ in endpoint.calls] == ["GET"]
    assert endpoint.store().status(RUN)["inbox_counts"] == {"delivered": 1}
    assert endpoint.store().status(RUN)["checkpoint"] == "Waiting for owner review"


def test_descriptor_has_versioned_object_schema_but_does_not_register_runtime():
    definition = owner_tool_definition()
    assert definition["name"] == TOOL_NAME
    schema = definition["parameters"]
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert schema["properties"]["version"]["const"] == 1
    assert set(schema["properties"]["request"]["discriminator"]["mapping"]) == {
        "lease",
        "report",
        "poll",
        "consume",
        "acknowledge",
    }
