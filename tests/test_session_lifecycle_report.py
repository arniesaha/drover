import json
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from drover.config import LifecycleConfig, load_config
from drover.schema import bootstrap
from drover.server.harness.lifecycle import LifecycleStore
from drover.server.harness.lifecycle_inventory import worktree_inventory
from drover.server.harness.lifecycle_report import session_decision
from drover.server.harness.models import HarnessSession
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector

NOW = datetime.now(timezone.utc)


def candidate():
    return HarnessSession(
        "s",
        "h",
        "codex",
        "codex",
        "running",
        mode="structured",
        awaiting="input",
        last_activity=NOW - timedelta(hours=12),
    )


def publication(state="merged", verified=NOW):
    return {"pr_state": state, "pr_verified_at": verified}


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"awaiting": "approval"}, "not_structured_input_wait"),
        ({"awaiting": None}, "not_structured_input_wait"),
        ({"status": "completed"}, "not_confirmed_running"),
        ({"status": "unknown"}, "not_confirmed_running"),
        ({"mode": "pty"}, "not_structured_input_wait"),
        ({"last_activity": None}, "unknown_or_future_activity"),
        ({"last_activity": NOW + timedelta(seconds=1)}, "unknown_or_future_activity"),
        (
            {"last_activity": NOW - timedelta(hours=12) + timedelta(seconds=1)},
            "within_idle_window",
        ),
        ({"retention_policy": "keep"}, "explicit_keep"),
    ],
)
def test_eligibility_fail_closed(changes, reason):
    decision = session_decision(
        replace(candidate(), **changes), [publication()], [], NOW, 43200
    )
    assert not decision["would_expire"]
    assert reason in decision["reasons"]


def test_boundary_pr_and_factory_protections():
    assert session_decision(candidate(), [publication()], [], NOW, 43200)[
        "would_expire"
    ]
    for pubs, owners, reason in [
        ([], [], "publication_evidence_missing"),
        ([publication("open")], [], "open_pr"),
        ([publication("unknown", None)], [], "pr_verification_unknown_or_stale"),
        (
            [publication(verified=NOW - timedelta(minutes=16))],
            [],
            "pr_verification_unknown_or_stale",
        ),
        ([publication()], None, "owner_lookup_unknown"),
        (
            [publication()],
            [
                {
                    "session_id": "s",
                    "owner_id": "owner",
                    "lease_until": NOW + timedelta(seconds=1),
                }
            ],
            "live_factory_owner",
        ),
    ]:
        result = session_decision(candidate(), pubs, owners, NOW, 43200)
        assert not result["would_expire"]
        assert reason in result["reasons"]


def test_config_and_enforce_rejection(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[lifecycle]\nmode="off"\nidle_after="6h"\n')
    assert load_config(path).lifecycle == LifecycleConfig("off", "6h")
    assert LifecycleConfig().idle_after_seconds == 43200
    with pytest.raises(ValueError, match="enforce is not wired"):
        LifecycleConfig("enforce")


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True
    ).stdout


def test_real_git_inventory_is_read_only_and_reports_foreign_and_symlink(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    root = home / ".drover" / "worktrees"
    root.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "base",
    )
    owned = root / "s"
    foreign = tmp_path / "foreign"
    git(repo, "worktree", "add", "-b", "drover/s", str(owned))
    git(repo, "worktree", "add", "-b", "other", str(foreign))
    (owned / "untracked.txt").write_text("preserve me")
    (root / "escape").symlink_to(foreign, target_is_directory=True)
    sessions = [
        replace(candidate(), cwd=str(owned)),
        replace(candidate(), session_id="f", cwd=str(root / "escape")),
    ]
    before = git(repo, "show-ref")
    import drover.server.harness.lifecycle_inventory as module

    original = module._git
    commands = []

    def inspect(path, *args):
        assert args[0] in ("rev-parse", "worktree", "status")
        if args[0] == "worktree":
            assert args[1:] == ("list", "--porcelain", "-z")
        commands.append(args)
        return original(path, *args)

    monkeypatch.setattr(module, "_git", inspect)
    result = worktree_inventory(sessions, home=home)
    entries = {r["path"]: r for r in result["worktrees"]}
    assert entries[str(owned)]["ownership"] == "owned"
    assert "local_changes" in entries[str(owned)]["reasons"]
    assert entries[str(foreign)]["ownership"] == "foreign"
    assert not any(t["would_collect"] for t in result["worktrees"])
    assert git(repo, "show-ref") == before
    assert (owned / "untracked.txt").read_text() == "preserve me"
    assert foreign.exists()
    assert commands


def test_report_scan_records_inventory_but_never_stops_or_collects(
    tmp_path, monkeypatch
):
    path = tmp_path / "control.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    registry = HarnessRegistry(path)
    registry.register_host(
        host_id="h",
        display_name="Host",
        kind="linux",
        capabilities={"lifecycle": {"worktree_inventory": 1}},
    )
    registry.register_host(host_id="old", display_name="Old", kind="linux")
    registry.create_session(
        session_id="s",
        host_id="h",
        harness="codex",
        command="codex",
        status="running",
        mode="structured",
    )
    registry.update_session_activity(
        "s", awaiting="input", last_activity=NOW - timedelta(hours=13)
    )
    store = LifecycleStore(path)
    store.report_publication(
        "s",
        dict(
            repo="owner/repo",
            pushed_branch="feature",
            pushed_sha="a" * 40,
            session_head="b" * 40,
            base_sha="c" * 40,
            pr_number=1,
            source="operator",
        ),
    )
    collector = MetricsCollector(
        duckdb_path=path, incoming_dir=tmp_path, summarizer_report={}
    )
    calls = []

    def request(host, route, **kwargs):
        assert route == "/lifecycle/worktrees" and kwargs["method"] == "GET"
        calls.append(route)
        return 200, json.dumps(
            {
                "worktrees": [
                    {
                        "path": "/example/worktree",
                        "session_id": "s",
                        "ownership": "owned",
                        "would_collect": True,
                        "reasons": [],
                    }
                ],
                "errors": [],
            }
        )

    monkeypatch.setattr(collector, "_harness_request", request)

    def forbidden(*args, **kwargs):
        pytest.fail("report policy dispatched stop")

    monkeypatch.setattr(LifecycleStore, "request_stop", forbidden)
    for _ in range(2):
        status, body = collector.lifecycle_report()
        result = json.loads(body)
        assert status == 200
        assert result["summary"]["unsupported_hosts"] == 1
        assert result["summary"]["would_expire"] == 0
        assert not result["hosts"][0]["worktrees"][0]["would_collect"]
    assert collector.lifecycle_health()["worktrees"] == 1
    with registry._connect() as con:
        assert con.execute("SELECT count(*) FROM session_worktrees").fetchone()[0] == 1
        assert (
            con.execute("SELECT count(*) FROM session_lifecycle_operations").fetchone()[
                0
            ]
            == 0
        )
        assert con.execute(
            "SELECT archived_at, ended_at FROM harness_sessions WHERE session_id='s'"
        ).fetchone() == (None, None)
    collector.lifecycle_config = LifecycleConfig("off")
    calls.clear()
    assert json.loads(collector.lifecycle_report()[1])["hosts"] == []
    assert calls == []


def test_api_health_detail_and_host_inventory_are_authenticated(tmp_path, monkeypatch):
    import threading
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from drover.server.harness.daemon import (
        DEFAULT_PRESETS,
        HarnessDaemonState,
        create_harness_server,
    )
    from drover.server.harness.pty import PtySessionManager
    from drover.server.metrics import start_metrics_server
    from drover.server.web.auth import AuthSettings

    path = tmp_path / "control.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    registry = HarnessRegistry(path)
    collector = MetricsCollector(
        duckdb_path=path, incoming_dir=tmp_path, summarizer_report={}
    )
    hub = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(enabled=True, api_token="test-lifecycle-token"),
    )
    state = HarnessDaemonState(
        host_id="h",
        display_name="Host",
        kind="linux",
        registry=registry,
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        api_token="test-lifecycle-token",
    )
    daemon = create_harness_server(state=state, listen_host="127.0.0.1", listen_port=0)
    thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    thread.start()
    import drover.server.harness.lifecycle_inventory as inventory_module

    monkeypatch.setattr(
        inventory_module,
        "worktree_inventory",
        lambda *a: {"worktrees": [], "errors": []},
    )
    try:
        for server, route in (
            (hub, "/harness/lifecycle"),
            (daemon, "/lifecycle/worktrees"),
        ):
            url = f"http://127.0.0.1:{server.server_port}{route}"
            with pytest.raises(HTTPError) as error:
                urlopen(url, timeout=3)
            assert error.value.code == 401
            with urlopen(
                Request(url, headers={"Authorization": "Bearer test-lifecycle-token"}),
                timeout=3,
            ) as response:
                assert response.status == 200
        with urlopen(
            f"http://127.0.0.1:{hub.server_port}/healthz?detail=1", timeout=3
        ) as response:
            assert json.load(response)["lifecycle"]["mode"] == "report"
    finally:
        for server in (hub, daemon):
            server.shutdown()
            server.server_close()


def test_host_updates_preserve_hub_policy(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    registry = HarnessRegistry(path)
    registry.create_session(
        session_id="s", host_id="h", harness="codex", command="codex"
    )
    with registry._connect() as con:
        con.execute(
            "UPDATE harness_sessions SET retention_policy='keep', retention_reason='design', retention_actor='operator' WHERE session_id='s'"
        )
    registry.update_session_status("s", "running")
    registry.update_session_activity("s", awaiting="input")
    row = registry.get_session("s")
    assert (row.retention_policy, row.retention_reason, row.retention_actor) == (
        "keep",
        "design",
        "operator",
    )


def test_periodic_reporter_stops_and_off_does_not_start():
    import threading
    from types import SimpleNamespace

    from drover.server.harness.lifecycle_report import start_reporter

    stop = threading.Event()
    calls = []

    def scan():
        calls.append("scan")
        stop.set()

    collector = SimpleNamespace(
        lifecycle_config=LifecycleConfig(), lifecycle_report=scan
    )
    thread = start_reporter(collector, stop)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert calls == ["scan"]
    collector.lifecycle_config = LifecycleConfig("off")
    assert start_reporter(collector, threading.Event()) is None
