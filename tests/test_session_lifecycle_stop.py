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
    "response", [404, 502, 504, "transport", 202, "invalid", "false", "accepted"]
)
def test_uncertain_stop_survives_restart_and_reconciles(setup, monkeypatch, response):
    path, registry, session, collector = setup

    def request(*args, **kwargs):
        if response == "transport":
            raise OSError("offline")
        if response == "invalid":
            return 200, "{}"
        if response == "accepted":
            return 202, json.dumps(
                {"session_id": session.session_id, "status": "terminated"}
            )
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
    monkeypatch.setattr(
        collector,
        "_harness_request",
        lambda *a, **k: pytest.fail("confirmed stop replayed"),
    )
    assert collector.proxy_terminate_harness_session(session.session_id)[0] == 200


def test_generation_and_host_fence(setup):
    path, registry, session, collector = setup
    store = LifecycleStore(path)
    op = store.request_stop(session.session_id)
    registry.mark_session_recovered(session.session_id, "native")
    assert not store.confirm(
        op, {"session_id": session.session_id, "status": "terminated"}
    )
    assert registry.get_session(session.session_id).status == "running"


def test_reconnect_replays_explicit_stop_after_confirming_live_session(
    setup, monkeypatch
):
    path, registry, session, collector = setup
    store = LifecycleStore(path)
    operation_id = store.request_stop(session.session_id)
    calls = []

    def request(host, route, **kwargs):
        calls.append(kwargs["method"])
        status = "running" if kwargs["method"] == "GET" else "terminated"
        return 200, json.dumps(
            {"session_id": session.session_id, "host_id": "h", "status": status}
        )

    monkeypatch.setattr(collector, "_harness_request", request)
    collector.reconcile_pending_stops("h")
    assert calls == ["GET", "POST"]
    assert not store.pending()
    assert registry.get_session(session.session_id).ended_at is not None


def test_postgres_stop_publication_inventory_roundtrip(pg_control_path):
    from datetime import datetime, timezone

    registry = HarnessRegistry(pg_control_path)
    registry.register_host(host_id="h", display_name="Host", kind="linux")
    registry.create_session(
        session_id="s", host_id="h", harness="shell", command="sh", status="running"
    )
    store = LifecycleStore(pg_control_path)
    op = store.request_stop("s")
    assert store.pending()[0][0] == op
    assert store.pending(host_id="h", session_id="s")[0][0] == op
    assert not store.confirm(
        op, {"session_id": "s", "host_id": "wrong", "status": "terminated"}
    )
    assert store.confirm(
        op, {"session_id": "s", "host_id": "h", "status": "terminated"}
    )
    assert not store.pending()
    assert registry.get_session("s").end_reason == "user"
    payload = dict(
        repo="owner/repo",
        pushed_branch="feature",
        pushed_sha="a" * 40,
        session_head="b" * 40,
        base_sha="c" * 40,
        source="operator",
    )
    assert store.report_publication("s", payload) == store.report_publication(
        "s", payload
    )
    tree = {
        "path": "/example/worktree",
        "session_id": "s",
        "ownership": "owned",
        "reasons": ["retained"],
    }
    store.record_inventory("h", [tree], datetime.now(timezone.utc))
    store.record_inventory("h", [tree], datetime.now(timezone.utc))
    with registry._connect() as con:
        assert con.execute("SELECT count(*) FROM session_worktrees").fetchone()[0] == 1
