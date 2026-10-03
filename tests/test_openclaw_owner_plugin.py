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
OWNER = "agent:coder:subagent:synthetic-owner"
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
        "canarySessionKey": PARENT,
        "authorityScope": "implementation",
    }
    try:
        yield store, event_id, config
    finally:
        server.shutdown()
        server.server_close()


def invoke(node, config, session, calls, *, token=TOKEN):
    # Only a synthetic test credential is supplied to the Node child; no host
    # credentials/config/production DSN are forwarded or resolved.
    return subprocess.run(
        [node, str(PLUGIN / "test/drive.js")],
        input=json.dumps({"config": config, "sessionKey": session, "calls": calls}),
        text=True,
        capture_output=True,
        timeout=15,
        cwd=PLUGIN,
        env={"DROVER_PLUGIN_TEST_TOKEN": token},
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
