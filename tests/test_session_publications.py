import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from drover.schema import bootstrap
from drover.server.harness.lifecycle import LifecycleStore
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector, start_metrics_server
from drover.server.web.auth import AuthSettings


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    registry = HarnessRegistry(path)
    for sid in ("s1", "s2"):
        registry.create_session(
            session_id=sid,
            host_id="h",
            harness="shell",
            command="sh",
            repo_owner="owner",
            repo_name="repo",
        )
    collector = MetricsCollector(
        duckdb_path=path, incoming_dir=tmp_path, summarizer_report={}
    )
    return LifecycleStore(path), collector


def report():
    return dict(
        repo="owner/repo",
        pushed_branch="feature/work",
        pushed_sha="a" * 40,
        session_head="b" * 40,
        base_sha="c" * 40,
        pr_number=12,
        source="orchestrator",
    )


def test_idempotent_many_to_many_immutable_snapshots(setup):
    store, _ = setup
    p = store.report_publication("s1", report())
    assert store.report_publication("s1", report()) == p
    store.report_publication("s2", report())
    store.report_publication("s1", dict(report(), pr_number=13, pushed_sha="d" * 40))
    assert len(store.publications("s1")) == 2
    assert len(store.publications("s2")) == 1
    assert p["pr_state"] == "unknown"
    assert p["pr_verified_at"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"repo": "https://user:password@example.com/owner/repo"},
        {"repo": "other/repo"},
        {"pushed_branch": "https://example.com/token"},
        {"source": "github"},
        {"pr_number": True},
        {"pushed_sha": "abc"},
        {"token": "secret"},
        {"pushed_branch": "ghp_" + "a" * 40},
    ],
)
def test_rejects_credentials_invalid_reports_and_wrong_repo(setup, change):
    store, _ = setup
    with pytest.raises(ValueError):
        store.report_publication("s1", dict(report(), **change))
    assert not store.publications("s1")


def test_publication_routes_require_auth_and_support_existing_clients(setup):
    store, collector = setup
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(enabled=True, api_token="test-publication-token"),
    )
    url = f"http://127.0.0.1:{server.server_port}/harness/sessions/s1/publications"
    try:
        for method in ("GET", "POST"):
            with pytest.raises(HTTPError) as error:
                urlopen(
                    Request(
                        url,
                        data=(
                            json.dumps(report()).encode() if method == "POST" else None
                        ),
                        method=method,
                    ),
                    timeout=3,
                )
            assert error.value.code == 401
        headers = {
            "Authorization": "Bearer test-publication-token",
            "Content-Type": "application/json",
        }
        with urlopen(
            Request(url, data=json.dumps(report()).encode(), headers=headers), timeout=3
        ) as response:
            assert response.status == 200
        with urlopen(Request(url, headers=headers), timeout=3) as response:
            assert len(json.load(response)["publications"]) == 1
    finally:
        server.shutdown()
        server.server_close()
