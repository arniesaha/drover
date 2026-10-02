"""HTTP routes for Session Graph and Project Activity (#473)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from drover.schema import bootstrap
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector
from drover.server.web.app import start_metrics_server


def _get(port: int, path: str) -> tuple[int, dict]:
    try:
        response = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read())


@pytest.fixture
def served(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    started = datetime.now(timezone.utc) - timedelta(minutes=20)
    for session_id, parent in (("orchestrator", None), ("worker", "orchestrator")):
        registry.create_session(
            session_id=session_id,
            host_id="mac-mini",
            harness="claude-code",
            command="claude",
            status="running",
            started_at=started,
            repo_owner="acme",
            repo_name="widget",
            parent_session_id=parent,
        )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
    )
    server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_session_graph_route_returns_the_delegation_tree(served) -> None:
    status, body = _get(served, "/harness/sessions/worker/graph")

    assert status == 200
    assert body["source"] == "drover_launch_metadata"
    top = body["root"]["children"][0]
    assert top["session_id"] == "orchestrator"
    assert [child["session_id"] for child in top["children"]] == ["worker"]


def test_session_graph_route_404s_for_unknown_targets(served) -> None:
    assert _get(served, "/harness/sessions/missing/graph")[0] == 404
    assert _get(served, "/harness/runs/missing-run/graph")[0] == 404


def test_project_activity_route_answers_from_drover_sessions(served) -> None:
    status, body = _get(served, "/projects/activity?project=acme/widget&days=3")

    assert status == 200
    assert body["source"] == "drover_sessions"
    assert body["projects"][0]["project_key"] == "acme/widget"
    assert body["projects"][0]["session_count"] == 2
    sessions = [s["session_id"] for day in body["days"] for s in day["sessions"]]
    assert sorted(sessions) == ["orchestrator", "worker"]


@pytest.mark.parametrize(
    "query",
    ["days=0", "days=31", "limit=201", "project=not-a-pair", "since=2026-01-01"],
)
def test_project_activity_route_rejects_unbounded_or_unknown_input(
    served, query
) -> None:
    status, body = _get(served, f"/projects/activity?{query}")

    assert status == 400
    assert "error" in body
