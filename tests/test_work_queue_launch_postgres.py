"""Postgres-backed contract for the real automatic queue launch path.

The fixture is a sanitized, structural export of the control rows involved in
launching work.  The sole fake is a small harnessd HTTP process; the store,
reconciler, service, bridge, collector session sync, and continuity projection
all use their production implementations.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from drover.server.harness.continuity import FactoryObserverContinuity
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector
from drover.schema import bootstrap
from drover.server.work_queue.config import WorkQueueConfig
from drover.server.work_queue.reconciler import QueueExecutionWorker
from drover.server.work_queue.runtime import HarnessBridge
from drover.server.work_queue.service import (
    WorkActor,
    WorkDenied,
    WorkService,
    identity,
)
from drover.server.work_queue.store import WorkStore
from drover.server.work_queue.provider import ItemRef

SNAPSHOT = Path(
    os.environ.get(
        "DROVER_LAUNCH_SNAPSHOT",
        Path(__file__).parent / "fixtures" / "work_queue_launch_snapshot.json",
    )
)
REF = ItemRef(
    "sanitized-launch-workspace",
    "github",
    "arniesaha/drover",
    866,
    "https://github.com/arniesaha/drover/issues/866",
)


class QueuedIssueProvider:
    actor_principal = "github:sanitized-launch-fixture"
    capabilities = frozenset({"issues_read", "issues_write"})

    def __init__(self):
        self.item = {
            "number": 866,
            "state": "open",
            "body": "Launch a trivial worker turn.",
            "created_at": "2026-10-27T00:00:00Z",
            "labels": [
                {"name": "status:queued"},
                {"name": "agent:nix"},
                {"name": "goal:launch"},
            ],
        }

    def authenticate(self):
        pass

    def list_items(self):
        return [self.item]

    def list_attention(self):
        return []

    def get_item(self, ref):
        return self.item

    def set_labels(self, ref, labels):
        self.item["labels"] = [{"name": label} for label in labels]


class StubHarnessd:
    """A process boundary fake with only create and inspect endpoints."""

    def __init__(self):
        self.sessions = {}
        self.requests = []

        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - HTTP handler API
                if self.path != "/sessions":
                    self.send_error(404)
                    return
                payload = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"])).decode()
                )
                session_id = "stub-session-" + str(len(stub.sessions) + 1)
                row = {
                    "session_id": session_id,
                    "client_session_id": payload["client_session_id"],
                    "harness": payload["harness"],
                    "mode": payload["mode"],
                    "status": "running",
                    "cwd": payload["cwd"],
                }
                stub.sessions[session_id] = row
                stub.requests.append(payload)
                self._json(201, row)

            def do_GET(self):  # noqa: N802 - HTTP handler API
                if self.path == "/providers/usage":
                    self._json(
                        200,
                        {
                            "accounts": [
                                {
                                    "snapshot_id": "stub-usage",
                                    "dedup_key": "stub-usage",
                                    "provider": "openai",
                                    "account_label": "sanitized fixture",
                                    "status": "ok",
                                    "observed_at": datetime.now(
                                        timezone.utc
                                    ).isoformat(),
                                    "source": "codex-app-server",
                                    "harnesses": ["codex"],
                                    "windows": [
                                        {
                                            "kind": "primary",
                                            "used_percent": 1,
                                            "window_minutes": 300,
                                        }
                                    ],
                                }
                            ]
                        },
                    )
                    return
                if not self.path.startswith("/sessions"):
                    self.send_error(404)
                    return
                self._json(200, {"sessions": list(stub.sessions.values())})

            def _json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):  # noqa: A003 - silence fixture HTTP
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%s" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def launch_path(pg_control_path):
    snapshot = json.loads(SNAPSHOT.read_text())
    harnessd = StubHarnessd()
    bootstrap(
        parquet_dir=pg_control_path.parent / "parquet", duckdb_path=pg_control_path
    )
    registry = HarnessRegistry(pg_control_path)
    host_id = snapshot["hosts"][0]["host_id"]
    for host in snapshot["hosts"]:
        registry.register_host(
            **host,
            local_url=harnessd.url,
            status="online",
        )
    config = WorkQueueConfig(
        enabled=True,
        shadow_mode=False,
        workspace_id=snapshot["workspace_id"],
        execution={"global_max_workers": 2, "orphan_intent_minutes": 10},
        goals=[
            {
                "goal_id": "launch",
                "repo": "arniesaha/drover",
                "goal_label": "goal:launch",
                "execution_enabled": True,
                "execution_cwds": {host_id: "/repo/drover"},
                "continuity_owner_id": "agent:owner:launch",
                "credential": {
                    "token_env": "SANITIZED_TOKEN",
                    "capabilities": ["issues_read", "issues_write"],
                },
            }
        ],
    )
    collector = MetricsCollector(
        duckdb_path=pg_control_path,
        incoming_dir=pg_control_path.parent / "incoming",
        summarizer_report={},
        api_token="test-hub-token",
        work_coordinator_continuity_secret="test-continuity-secret",
    )
    bridge = HarnessBridge(collector, config)
    provider = QueuedIssueProvider()
    service = WorkService(
        config,
        WorkStore(pg_control_path, config.workspace_id),
        {"launch": provider},
        bridge,
    )
    collector.work_control = service
    try:
        yield service, bridge, registry, provider, harnessd
    finally:
        harnessd.close()


def test_queued_issue_launches_worker_and_records_owner_continuation(launch_path):
    service, _bridge, registry, _provider, harnessd = launch_path
    worker = QueueExecutionWorker(service)
    worker.reconciler.providers["launch"] = service.providers["launch"]

    worker.process_once()

    link = service.store.links()[0]
    assert link["session_id"] == "stub-session-1"
    assert harnessd.requests[0]["client_session_id"] == link["run_key"]
    session = registry.get_session(link["session_id"])
    assert session and session.handoff_mode == "work_coordinator"

    # This is the trivial worker turn crossing the normal harness event ingest
    # boundary.  Its declared state must become a durable owner continuation.
    registry.append_event(
        session_id=link["session_id"],
        event_id="trivial-turn",
        event_type="assistant_output",
        payload={"text": "Trivial turn complete.\nDrover-State: done sha=" + "a" * 40},
        seq=1,
    )
    run = FactoryObserverContinuity(service.store.path).status(
        "work-" + link["run_key"]
    )
    assert [event["report"]["kind"] for event in run["events"]] == [
        "worker_started",
        "worker_done",
    ]


def test_failed_intent_refreshes_age_before_relaunch(launch_path):
    service, _bridge, _registry, _provider, harnessd = launch_path
    request_key = "failed-intent"
    run_key = identity(service.config.workspace_id, "launch", 866, request_key, "work")
    row = service.store.intent(
        "control:test",
        REF,
        "launch",
        run_key,
        "work",
        0,
        "goal:launch",
    )
    with service.store.connection() as con:
        con.execute(
            "UPDATE execution_links SET created_at = now() - interval '11 minutes', "
            "updated_at = now() - interval '11 minutes' WHERE id = ?",
            [row["id"]],
        )
    service.store.failure(run_key, 1)
    refreshed = service.store.links(run_key=run_key)[0]
    assert refreshed["updated_at"] > refreshed["created_at"] + timedelta(minutes=10)
    with pytest.raises(WorkDenied, match="launch intent uncertain"):
        service.launch(
            "launch",
            866,
            request_key,
            5,
            actor=WorkActor("control:queue-execution", "nix"),
        )
    with service.store.connection() as con:
        con.execute(
            "UPDATE execution_links SET updated_at = now() - interval '11 minutes' WHERE id = ?",
            [row["id"]],
        )
    relaunch = service.launch(
        "launch", 866, request_key, 5, actor=WorkActor("control:queue-execution", "nix")
    )
    assert relaunch["session_id"] == "stub-session-1"
    assert relaunch["failure_count"] == 2
    assert len(harnessd.requests) == 1


def test_sessionless_orphan_stays_denied_until_its_refreshed_window_expires(
    launch_path,
):
    service, _bridge, _registry, _provider, _harnessd = launch_path
    row = service.store.intent(
        "control:test",
        REF,
        "launch",
        "fresh-orphan",
        "work",
        0,
        "goal:launch",
    )
    with service.store.connection() as con:
        con.execute(
            "UPDATE execution_links SET created_at = now() - interval '11 minutes', "
            "updated_at = now() - interval '1 minute' WHERE id = ?",
            [row["id"]],
        )
    with pytest.raises(Exception, match="existing execution requires inspection"):
        service.launch(
            "launch",
            866,
            "new-request",
            5,
            actor=WorkActor("control:queue-execution", "nix"),
        )


def test_resolve_skips_detached_relay_and_finds_the_reachable_host(launch_path):
    service, bridge, registry, _provider, harnessd = launch_path
    registry.register_host(
        host_id="detached-relay",
        display_name="Detached relay",
        kind="linux",
        connection_kind="relay",
        capabilities={"harnesses": []},
        status="online",
    )
    harnessd.sessions["remote-session"] = {
        "session_id": "remote-session",
        "client_session_id": "remote-key",
        "harness": "codex",
        "status": "running",
    }
    service.store.intent(
        "control:test",
        REF,
        "launch",
        "remote-key",
        "work",
        0,
        "goal:launch",
    )

    session = bridge.resolve("remote-key")

    assert session and session.session_id == "remote-session"


def test_stale_intent_with_detached_relay_stays_denied_without_relaunch(launch_path):
    service, _bridge, registry, _provider, harnessd = launch_path
    registry.register_host(
        host_id="detached-relay",
        display_name="Detached relay",
        kind="linux",
        connection_kind="relay",
        capabilities={"harnesses": []},
        status="online",
    )
    request_key = "uninspectable-intent"
    run_key = identity(service.config.workspace_id, "launch", 866, request_key, "work")
    row = service.store.intent(
        "control:test",
        REF,
        "launch",
        run_key,
        "work",
        0,
        "goal:launch",
    )
    with service.store.connection() as con:
        con.execute(
            "UPDATE execution_links SET created_at = now() - interval '11 minutes', "
            "updated_at = now() - interval '11 minutes' WHERE id = ?",
            [row["id"]],
        )

    with pytest.raises(WorkDenied, match="harness inspection unavailable"):
        service.launch(
            "launch",
            866,
            request_key,
            5,
            actor=WorkActor("control:queue-execution", "nix"),
        )

    assert service.store.links(run_key=run_key)[0]["failure_count"] == 0
    assert harnessd.requests == []
