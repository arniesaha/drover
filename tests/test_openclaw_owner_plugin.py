"""Inactive plugin's fake SDK registration with real authenticated Drover HTTP.

The fake uses the NAS 2026.9.6 V2 factory/guard shape; a separate Node test
executes current NAS source registration. No Gateway, messaging, plugin
installation or configuration is used.
Tests require the standalone artifact's Node dependencies installed locally.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drover.schema import bootstrap_control_plane_store
from drover.server.harness.continuity import FactoryObserverContinuity
from drover.server.harness.openclaw_owner import OwnerToolCall, OwnerToolReply
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings

PLUGIN = Path(__file__).resolve().parents[1] / "plugins/openclaw-continuity-owner"
RUN = "run_PLUGIN_CANARY497"
OWNER = "agent:coder:synthetic-owner"
OWNER_PRINCIPAL = "legacy:operator"
PARENT = "agent:main:synthetic-parent"
TOKEN = "synthetic-plugin-only-test-token"


@pytest.fixture
def node():
    executable = shutil.which("node")
    if executable is None:
        pytest.skip(
            "Compatible Node unavailable for the OpenClaw 2026.9.6 plugin proof"
        )
    version = (
        subprocess.run(
            [executable, "--version"], capture_output=True, text=True, check=True
        )
        .stdout.strip()
        .removeprefix("v")
    )
    major, minor, *_ = [int(part) for part in version.split(".")]
    if not (
        (major == 24 and minor >= 16) or major > 26 or (major == 26 and minor >= 1)
    ):
        pytest.skip(f"Node {version} does not satisfy OpenClaw 2026.9.6 engine range")
    if not (PLUGIN / "node_modules/ajv/package.json").is_file():
        pytest.skip(
            "OpenClaw plugin dependencies missing; run npm ci --ignore-scripts in plugins/openclaw-continuity-owner"
        )
    return executable


@pytest.fixture
def canary(tmp_path):
    path = tmp_path / "canary.duckdb"
    bootstrap_control_plane_store(path)
    # Synthetic observer metadata only; no process or second worker launched.
    registry = HarnessRegistry(path)
    registry.create_session(
        session_id="synthetic-canary-observer",
        host_id="studio",
        harness="codex",
        command="never-executed",
        handoff_mode="factory_observer",
        source_session_id=f"factory/{RUN}@4",
    )
    store = FactoryObserverContinuity(path)
    store.initialize(
        session_id="synthetic-canary-observer",
        objective="Isolated read-only canary",
        checkpoint="No owner action acknowledged",
        authorized_owner_id=OWNER_PRINCIPAL,
    )
    event_id = store.report(
        RUN,
        source="openclaw",
        source_event_id="canary:worker:completed:1",
        subject="synthetic-worker",
        sequence=1,
        kind="worker_brief_completed",
        summary="Synthetic advisory commit for owner review only",
    )
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=MetricsCollector(
            duckdb_path=path,
            incoming_dir=tmp_path / "incoming",
            summarizer_report={},
            ttl_seconds=60,
        ),
        auth=AuthSettings(enabled=True, api_token=TOKEN),
    )
    config = {
        "enabled": True,
        "mode": "poll_only",
        "droverOrigin": f"http://127.0.0.1:{server.server_address[1]}",
        "tokenEnvName": "DROVER_PLUGIN_TEST_TOKEN",
        "runId": RUN,
        "ownerSessionKey": OWNER,
        "ownerPrincipal": OWNER_PRINCIPAL,
        "canarySessionKey": PARENT,
        "authorityScope": "implementation",
    }
    try:
        yield store, event_id, config
    finally:
        server.shutdown()
        server.server_close()


def invoke(node, config, session, calls, *, token=TOKEN, handoff_token=None):
    # Only a synthetic test credential is supplied to the Node child; no host
    # credentials/config/production DSN are forwarded or resolved.
    return subprocess.run(
        [node, str(PLUGIN / "test/drive.js")],
        input=json.dumps({"config": config, "sessionKey": session, "calls": calls}),
        text=True,
        capture_output=True,
        timeout=15,
        cwd=PLUGIN,
        env={
            "DROVER_PLUGIN_TEST_TOKEN": token,
            **(
                {"DROVER_PLUGIN_TEST_HANDOFF_TOKEN": handoff_token}
                if handoff_token is not None
                else {}
            ),
        },
    )


def payload(operation, **fields):
    return {"version": 1, "request": {"operation": operation, **fields}}


def results(process):
    assert process.returncode == 0, process.stderr
    output = json.loads(process.stdout)
    for reply in output:
        assert json.loads(reply["content"][0]["text"]) == reply["details"]
    return [reply["details"] for reply in output]


def test_generated_plugin_schema_matches_existing_strict_python_contract():
    for filename, model in (
        ("request.json", OwnerToolCall),
        ("reply.json", OwnerToolReply),
    ):
        assert (
            json.loads((PLUGIN / "schemas" / filename).read_text())
            == model.model_json_schema()
        )


def test_canary_parent_owner_poll_receipts_do_not_consume_or_ack(node, canary):
    store, event_id, config = canary
    before = store.status(RUN)
    for session in (PARENT, OWNER):
        [receipt] = results(invoke(node, config, session, [payload("poll", limit=1)]))
        assert receipt["continuity"]["events"][0]["event_id"] == event_id
        assert receipt["continuity"]["inbox_counts"] == {"pending": 1}
        assert receipt["delivery"] is None
    assert store.status(RUN) == before
    denied = invoke(node, config, OWNER, [payload("consume", owner_epoch=1)])
    assert denied.returncode != 0 and "Read-only canary" in denied.stderr
    assert store.status(RUN) == before


def test_authenticated_normal_http_explicit_owner_ack_and_store_reconstruction(
    node, canary
):
    store, event_id, canary_config = canary
    # Local fake SDK config only: this does not activate any installed plugin.
    config = {**canary_config, "mode": "owner"}
    [lease] = results(invoke(node, config, OWNER, [payload("lease")]))
    epoch = lease["continuity"]["owner"]["epoch"]
    [delivery] = results(
        invoke(node, config, OWNER, [payload("consume", owner_epoch=epoch)])
    )
    assert delivery["delivery"]["type"] == "review_worker_result"
    assert delivery["delivery"]["authorized"]
    assert store.status(RUN)["inbox_counts"] == {"delivered": 1}
    # A parent's canary binding remains read-only even in owner mode.
    denied = invoke(
        node,
        config,
        PARENT,
        [
            payload(
                "acknowledge",
                owner_epoch=epoch,
                event_id=event_id,
                checkpoint="Parent tried to impersonate owner",
            )
        ],
    )
    assert denied.returncode != 0 and "Read-only canary" in denied.stderr
    [ack] = results(
        invoke(
            node,
            config,
            OWNER,
            [
                payload(
                    "acknowledge",
                    owner_epoch=epoch,
                    event_id=event_id,
                    checkpoint="Owner explicitly reviewed synthetic advisory",
                )
            ],
        )
    )
    assert ack["continuity"]["inbox_counts"] == {"acknowledged": 1}
    reconstructed = FactoryObserverContinuity(store.path)
    assert (
        reconstructed.status(RUN)["checkpoint"]
        == "Owner explicitly reviewed synthetic advisory"
    )
    assert not reconstructed.status(RUN)["terminal_release"]
    [empty] = results(
        invoke(node, config, OWNER, [payload("consume", owner_epoch=epoch)])
    )
    assert empty["delivery"] is None


def test_real_drover_authentication_refuses_bad_credential_without_side_effects(
    node, canary
):
    store, _, config = canary
    before = store.status(RUN)
    denied = invoke(
        node, config, OWNER, [payload("poll")], token="wrong-synthetic-token"
    )
    assert denied.returncode != 0 and "HTTP 401" in denied.stderr
    assert "wrong-synthetic-token" not in denied.stderr
    assert store.status(RUN) == before


def test_owner_plugin_recovers_bulk_pending_completion_before_acknowledgement(
    node, tmp_path
):
    """A trusted owner can redeem/replay initialization without losing a batch fact."""
    run = "run_PLUGIN_PENDING498"
    session_id = "synthetic-pending-observer"
    other_run = "run_PLUGIN_OTHER498"
    other_session_id = "synthetic-other-observer"
    path = tmp_path / "pending.duckdb"
    bootstrap_control_plane_store(path)
    registry = HarnessRegistry(path)
    registry.create_session(
        session_id=session_id,
        host_id="studio",
        harness="codex",
        command="never-executed",
        handoff_mode="factory_observer",
        source_session_id=f"factory/{run}@4",
    )
    store = FactoryObserverContinuity(path)
    store.initialize(
        session_id=session_id,
        objective="Recover a retained completion",
        checkpoint="Awaiting authorized owner",
    )
    registry.create_session(
        session_id=other_session_id,
        host_id="studio",
        harness="codex",
        command="never-executed",
        handoff_mode="factory_observer",
        source_session_id=f"factory/{other_run}@4",
    )
    store.initialize(
        session_id=other_session_id,
        objective="Recover a retained completion",
        checkpoint="Awaiting authorized owner",
    )
    assert (
        registry.ingest_structured_events(
            [
                {
                    "event_id": "plugin-pending-batch-1",
                    "session_id": session_id,
                    "event_type": "status",
                    "payload": {"payload": {"turn_complete": True}},
                    "seq": 1,
                    "created_at": datetime.now(timezone.utc),
                }
            ]
        )
        == 1
    )
    assert store.status(run)["completion_receipts"]["counts"] == {
        "pending_authorization": 1
    }
    handoff_token = store.issue_handoff(
        session_id=session_id,
        objective="Recover a retained completion",
        checkpoint="Awaiting authorized owner",
        authorized_owner_id=OWNER_PRINCIPAL,
        issued_by="legacy:operator",
    )["handoff_token"]
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=MetricsCollector(
            duckdb_path=path,
            incoming_dir=tmp_path / "incoming",
            summarizer_report={},
            ttl_seconds=60,
        ),
        auth=AuthSettings(enabled=True, api_token=TOKEN),
    )
    config = {
        "enabled": True,
        "mode": "owner",
        "droverOrigin": f"http://127.0.0.1:{server.server_address[1]}",
        "tokenEnvName": "DROVER_PLUGIN_TEST_TOKEN",
        "handoffTokenEnvName": "DROVER_PLUGIN_TEST_HANDOFF_TOKEN",
        "runId": run,
        "observerSessionId": session_id,
        "objective": "Recover a retained completion",
        "checkpoint": "Awaiting authorized owner",
        "ownerSessionKey": OWNER,
        "ownerPrincipal": OWNER_PRINCIPAL,
        "authorityScope": "implementation",
    }
    try:
        forged = invoke(
            node,
            {**config, "runId": other_run, "observerSessionId": other_session_id},
            OWNER,
            [payload("initialize")],
            handoff_token=handoff_token,
        )
        assert forged.returncode != 0
        assert "HTTP 403" in forged.stderr
        assert handoff_token not in forged.stderr
        assert store.status(other_run)["owner_handoff"]["authorized_owner_id"] is None
        [initialized] = results(
            invoke(
                node,
                config,
                OWNER,
                [payload("initialize")],
                handoff_token=handoff_token,
            )
        )
        assert initialized["continuity"]["completion_receipts"]["counts"] == {
            "projected": 1
        }
        [lease] = results(invoke(node, config, OWNER, [payload("lease")]))
        epoch = lease["continuity"]["owner"]["epoch"]
        [delivery] = results(
            invoke(node, config, OWNER, [payload("consume", owner_epoch=epoch)])
        )
        event_id = delivery["delivery"]["event_id"]
        [acknowledged] = results(
            invoke(
                node,
                config,
                OWNER,
                [
                    payload(
                        "acknowledge",
                        owner_epoch=epoch,
                        event_id=event_id,
                        checkpoint="Owner explicitly acknowledged recovered completion",
                    )
                ],
            )
        )
        assert acknowledged["continuity"]["inbox_counts"] == {"acknowledged": 1}
        [replayed] = results(
            invoke(
                node,
                config,
                OWNER,
                [payload("initialize")],
                handoff_token=handoff_token,
            )
        )
        assert replayed["continuity"]["completion_receipts"]["counts"] == {
            "projected": 1
        }
        assert replayed["continuity"]["inbox_counts"] == {"acknowledged": 1}
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("scope", ["integration", "deployment"])
def test_owner_plugin_initialize_preserves_trusted_scope_on_postgres(
    node, pg_control_path, tmp_path, scope
):
    """The local Node plugin carries its configured scope to real PostgreSQL."""
    run = f"run_PLUGIN_PG_{scope.upper()}"
    session_id = f"plugin-pg-{scope}"
    registry = HarnessRegistry(pg_control_path)
    registry.create_session(
        session_id=session_id,
        host_id="studio",
        harness="codex",
        command="never-executed",
        handoff_mode="factory_observer",
        source_session_id=f"factory/{run}@4",
    )
    store = FactoryObserverContinuity(pg_control_path)
    store.initialize(
        session_id=session_id,
        objective="Initialize trusted scope",
        checkpoint="Pending",
        authority_scope=scope,
    )
    handoff_token = store.issue_handoff(
        session_id=session_id,
        objective="Initialize trusted scope",
        checkpoint="Pending",
        authority_scope=scope,
        authorized_owner_id=OWNER_PRINCIPAL,
        issued_by="legacy:operator",
    )["handoff_token"]
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=MetricsCollector(
            duckdb_path=pg_control_path,
            incoming_dir=tmp_path / "incoming",
            summarizer_report={},
            ttl_seconds=60,
        ),
        auth=AuthSettings(enabled=True, api_token=TOKEN),
    )
    config = {
        "enabled": True,
        "mode": "owner",
        "droverOrigin": f"http://127.0.0.1:{server.server_address[1]}",
        "tokenEnvName": "DROVER_PLUGIN_TEST_TOKEN",
        "handoffTokenEnvName": "DROVER_PLUGIN_TEST_HANDOFF_TOKEN",
        "runId": run,
        "observerSessionId": session_id,
        "objective": "Initialize trusted scope",
        "checkpoint": "Pending",
        "ownerSessionKey": OWNER,
        "ownerPrincipal": OWNER_PRINCIPAL,
        "authorityScope": scope,
    }
    try:
        [initialized] = results(
            invoke(
                node,
                config,
                OWNER,
                [payload("initialize")],
                handoff_token=handoff_token,
            )
        )
        assert initialized["continuity"]["authority_scope"] == scope
        assert initialized["continuity"]["owner_handoff"]["authorized_owner_id"] == (
            OWNER_PRINCIPAL
        )
    finally:
        server.shutdown()
        server.server_close()


def test_owner_runs_lists_only_actionable_runs_of_that_authorized_owner(canary):
    store, _event_id, _config = canary
    assert store.owner_runs(OWNER_PRINCIPAL) == [RUN]
    assert store.owner_runs(OWNER) == []
    with pytest.raises(ValueError):
        store.owner_runs(OWNER, limit=0)


def test_owner_listing_endpoint_is_exclusive_with_run_id_and_session_bound(
    node, canary
):
    import urllib.error
    import urllib.request

    _store, _event_id, config = canary

    def get(query):
        request = urllib.request.Request(
            f"{config['droverOrigin']}/harness/factory-observer/continuity?{query}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    assert get(f"owner_id={OWNER_PRINCIPAL}") == {"owner_runs": [RUN]}
    assert get(f"owner_id={OWNER}") == {"owner_runs": []}
    for query in (
        f"owner_id={OWNER}&run_id={RUN}",
        "limit=1",
        f"run_id={RUN}&offset=1",
        f"run_id={RUN}&authority_scope=implementation",
    ):
        with pytest.raises(urllib.error.HTTPError) as error:
            get(query)
        assert error.value.code == 400


def test_run_id_may_ride_a_call_only_when_it_matches_the_trusted_run(node, canary):
    _store, _event_id, config = canary
    [receipt] = results(invoke(node, config, OWNER, [payload("poll", run_id=RUN)]))
    assert receipt["continuity"]["run_id"] == RUN
    denied = invoke(node, config, OWNER, [payload("poll", run_id="run_OTHER")])
    assert denied.returncode != 0 and "trusted fixed run" in denied.stderr


def test_owner_runs_scope_filter_and_paging(canary):
    store, _event_id, _config = canary
    assert store.owner_runs(OWNER_PRINCIPAL, authority_scope="implementation") == [RUN]
    assert store.owner_runs(OWNER_PRINCIPAL, authority_scope="integration") == []
    assert store.owner_runs(OWNER_PRINCIPAL, offset=1) == []
    with pytest.raises(ValueError):
        store.owner_runs(OWNER_PRINCIPAL, authority_scope="bogus")


def test_owner_listing_is_limited_to_the_owner_principal_or_a_coordinator(tmp_path):
    import urllib.error
    import urllib.request

    from drover.server.web.credentials import CredentialStore

    path = tmp_path / "listing.duckdb"
    bootstrap_control_plane_store(path)
    credentials = CredentialStore(tmp_path / "credentials.json")
    record, token = credentials.issue(scope="host", label="reader", host_id="studio")
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=MetricsCollector(
            duckdb_path=path,
            incoming_dir=tmp_path / "incoming",
            summarizer_report={},
            ttl_seconds=60,
        ),
        auth=AuthSettings(enabled=True, api_token="", credentials=credentials),
    )

    def get(owner):
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}"
            f"/harness/factory-observer/continuity?owner_id={owner}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    try:
        assert get(f"credential:{record.id}") == {"owner_runs": []}
        with pytest.raises(urllib.error.HTTPError) as error:
            get(OWNER)
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
