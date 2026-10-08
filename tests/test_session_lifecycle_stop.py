import json
from dataclasses import replace

import pytest

from drover.schema import bootstrap
from drover.server.harness.lifecycle import LifecycleStore
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    registry = HarnessRegistry(path)
    registry.register_host(host_id="h", display_name="Host", kind="linux")
    session = registry.create_session(
        host_id="h", harness="shell", command="sh", status="running"
    )
    collector = MetricsCollector(
        duckdb_path=path, incoming_dir=tmp_path, summarizer_report={}
    )
    return path, registry, session, collector


@pytest.mark.parametrize(
    "response", [404, 502, 504, "transport", 202, "invalid", "false"]
)
def test_uncertain_stop_survives_restart_and_reconciles(setup, monkeypatch, response):
    path, registry, session, collector = setup

    def request(*args, **kwargs):
        if response == "transport":
            raise OSError("offline")
        if response == "invalid":
            return 200, "{}"
        if response == "false":
            return 200, json.dumps(
                {
                    "session_id": session.session_id,
                    "status": "terminated",
                    "terminated": False,
                }
            )
        return response, "{}"

    monkeypatch.setattr(collector, "_harness_request", request)
    status, body = collector.proxy_terminate_harness_session(session.session_id)
    assert status == 202
    op = json.loads(body)["operation_id"]
    assert registry.get_session(session.session_id).status == "running"
    assert registry.get_session(session.session_id).ended_at is None
    store = LifecycleStore(path)
    assert store.request_stop(session.session_id) == op
    monkeypatch.setattr(
        collector,
        "_harness_request",
        lambda *a, **k: (
            200,
            json.dumps({"session_id": session.session_id, "status": "terminated"}),
        ),
    )
    collector.reconcile_pending_stops("h")
    assert not store.pending()
    row = registry.get_session(session.session_id)
    assert row.status == "terminated"
    assert row.ended_at is not None
    assert row.end_reason == "user"


def test_generation_and_host_fence(setup):
    path, registry, session, collector = setup
    store = LifecycleStore(path)
    op = store.request_stop(session.session_id)
    registry.mark_session_recovered(session.session_id, "native")
    assert not store.confirm(
        op, {"session_id": session.session_id, "status": "terminated"}
    )
    assert registry.get_session(session.session_id).status == "running"
