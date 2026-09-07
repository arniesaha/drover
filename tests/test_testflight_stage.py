"""Local deployment boundaries; subprocesses and HTTP never reach a real host."""

import hashlib
import io
import json
import plistlib
import subprocess
import tomllib
from pathlib import Path

import pytest
from testflight import stage

SHA = "a" * 40
OLDER = "b" * 40


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / "stage"
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = []

    def run(argv, **kwargs):
        argv = [str(arg) for arg in argv]
        calls.append((argv, kwargs))
        output = ""
        if "rev-parse" in argv:
            output = argv[-1].removesuffix("^{commit}") + "\n"
            if argv[-1] == "HEAD":
                output = Path(argv[argv.index("-C") + 1]).name + "\n"
        if "worktree" in argv:
            Path(argv[-2]).mkdir(parents=True)
        if "importlib.metadata" in " ".join(argv):
            output = "0.4.13\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(stage.subprocess, "run", run)
    monkeypatch.setattr(stage.time, "sleep", lambda _: None)
    return root, repo, calls


def prepare(runtime, sha=SHA):
    root, repo, _ = runtime
    stage.prepare(repo, root, sha, "https://stage.example.test")
    return root


@pytest.mark.parametrize("sha", ["HEAD", "a" * 39, "g" * 40, "../release"])
def test_prepare_rejects_non_commit_sha(runtime, sha):
    with pytest.raises(stage.StageError):
        prepare(runtime, sha)
    assert runtime[2] == []


@pytest.mark.parametrize(
    "url",
    [
        "http://stage.example.test",
        "https://u:p@stage.example.test",
        "https://stage.example.test/path",
        "https://stage.example.test?token=x",
    ],
)
def test_prepare_rejects_non_https_origin(runtime, url):
    root, repo, calls = runtime
    with pytest.raises(stage.StageError):
        stage.prepare(repo, root, SHA, url)
    assert calls == []


def test_rejects_relative_and_symlink_roots(runtime):
    root, repo, calls = runtime
    for unsafe in [Path("relative"), root]:
        if unsafe == root:
            root.symlink_to(repo, target_is_directory=True)
        with pytest.raises(stage.StageError):
            stage.prepare(repo, unsafe, SHA, "https://stage.example.test")
    assert calls == []


@pytest.mark.parametrize("dirty,reachable", [(True, True), (False, False)])
def test_prepare_requires_clean_repository_and_origin_main(
    runtime, monkeypatch, dirty, reachable
):
    original = stage.subprocess.run

    def run(argv, **kwargs):
        if "status" in argv and dirty:
            return subprocess.CompletedProcess(argv, 0, " M tracked.py\n", "")
        if "merge-base" in argv and not reachable:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return original(argv, **kwargs)

    monkeypatch.setattr(stage.subprocess, "run", run)
    with pytest.raises(stage.StageError):
        prepare(runtime)
    assert not runtime[0].exists()


def test_prepare_renders_isolated_private_artifacts(runtime, monkeypatch):
    monkeypatch.setenv("DROVER_API_TOKEN", "personal-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "personal-provider-secret")
    root = prepare(runtime)
    cfg = tomllib.loads((root / "home/.drover/config.toml").read_text())
    assert cfg["paths"] == {
        "incoming_dir": str(root / "state/incoming"),
        "parquet_dir": str(root / "state/parquet"),
        "duckdb_path": str(root / "state/drover.duckdb"),
    }
    assert cfg["server"]["metrics_host"] == "127.0.0.1"
    assert cfg["server"]["metrics_http_port"] == 17080
    assert cfg["auth"]["enabled"] is True
    assert not cfg["auth"].get("api_token")
    for name in ["server", "harnessd"]:
        path = root / "launchd" / f"com.drover.testflight-{name}.plist"
        job = plistlib.loads(path.read_bytes())
        env = job["EnvironmentVariables"]
        assert env["HOME"] == str(root / "home")
        assert env["DROVER_RELEASE_ROLE"] == "testflight-staging"
        assert env["DROVER_RELEASE_SHA"] == SHA
        assert env["DROVER_STAGING_ATTESTATION_PATH"] == str(
            root / "staging-probe.json"
        )
        args = job["ProgramArguments"]
        # env -i also excludes launchd-manager environment and provider overrides.
        assert args[:2] == ["/usr/bin/env", "-i"]
        assert str(root / "worktrees" / SHA / ".venv/bin" / f"drover-{name}") in args
        assert "--host-token" not in args
        if name == "server":
            assert "--no-otlp" in args and "--no-mcp" in args
        else:
            assert args[args.index("--listen") + 1] == "127.0.0.1:17081"
            assert args[args.index("--local-url") + 1] == "http://127.0.0.1:17081"
        assert path.stat().st_mode & 0o777 == 0o600
        assert "personal-secret" not in path.read_text()
    record = root / "active-release.json"
    assert json.loads(record.read_text())["package_version"] == "0.4.13"
    assert record.stat().st_mode & 0o777 == 0o600
    sync = next(call for call in runtime[2] if call[0][:2] == ["uv", "sync"])
    assert sync[0] == ["uv", "sync", "--frozen", "--no-dev"]
    assert sync[1]["env"]["HOME"] == str(root / "home")
    assert "DROVER_API_TOKEN" not in sync[1]["env"]


def healthy_http(url, *, method="GET", token=None, payload=None):
    if url.endswith("/release-identity"):
        return {
            "role": "testflight-staging",
            "source_sha": SHA,
            "package_version": "0.4.13",
            "staging_probe": None,
        }
    if url.endswith("/harness/hosts"):
        return {
            "hosts": [
                {
                    "host_id": "testflight-staging-mac-mini",
                    "status": "online",
                    "local_url": "http://127.0.0.1:17081",
                }
            ]
        }
    if url.endswith("/readyz"):
        return {"ready": True, "stores": {}}
    return {"ok": True, "host_id": "testflight-staging-mac-mini", "active_sessions": 0}


def ready_root(runtime, monkeypatch):
    root = prepare(runtime)
    token = root / "home/.drover/api_token"
    token.write_text("stage-only-token")
    token.chmod(0o600)
    monkeypatch.setattr(stage, "http", healthy_http)
    return root


def test_activate_orders_jobs_and_rollback_only_restarts_staging(runtime, monkeypatch):
    root = ready_root(runtime, monkeypatch)
    for action in [stage.activate, stage.rollback]:
        runtime[2].clear()
        action(root, SHA)
        launches = [argv for argv, _ in runtime[2] if argv[0] == "launchctl"]
        bootstraps = [argv for argv in launches if argv[1] == "bootstrap"]
        assert [Path(argv[-1]).name for argv in bootstraps] == [
            "com.drover.testflight-server.plist",
            "com.drover.testflight-harnessd.plist",
        ]
        assert all(
            "com.drover.server" not in " ".join(argv)
            and "com.drover.harnessd" not in " ".join(argv)
            for argv in launches
        )


def test_failed_readiness_never_boots_harness(runtime, monkeypatch):
    root = ready_root(runtime, monkeypatch)

    def failure(url, **kwargs):
        if url.endswith("/readyz"):
            raise stage.StageError("not ready")
        return healthy_http(url, **kwargs)

    monkeypatch.setattr(stage, "http", failure)
    with pytest.raises(stage.StageError):
        stage.activate(root, SHA)
    assert not any(
        argv[0:2] == ["launchctl", "bootstrap"] and argv[-1].endswith("harnessd.plist")
        for argv, _ in runtime[2]
    )


@pytest.mark.parametrize(
    "replacement", ["metrics_http_port = 17082", 'metrics_host = "0.0.0.0"']
)
def test_changed_listener_config_is_rejected_before_launch(
    runtime, monkeypatch, replacement
):
    root = ready_root(runtime, monkeypatch)
    path = root / "home/.drover/config.toml"
    original = (
        "metrics_http_port = 17080"
        if "port" in replacement
        else 'metrics_host = "127.0.0.1"'
    )
    path.write_text(path.read_text().replace(original, replacement))
    runtime[2].clear()
    with pytest.raises(stage.StageError):
        stage.activate(root, SHA)
    assert not any(argv[0] == "launchctl" for argv, _ in runtime[2])


@pytest.mark.parametrize(
    "role,text,success",
    [
        ("assistant", " DROVER_TESTFLIGHT_STAGE_OK\n", True),
        ("assistant", "DROVER_TESTFLIGHT_STAGE_OK extra", False),
        ("user", "DROVER_TESTFLIGHT_STAGE_OK", False),
    ],
)
def test_probe_attests_only_exact_assistant_response(
    runtime, monkeypatch, role, text, success
):
    root = ready_root(runtime, monkeypatch)
    target = root / "staging-probe.json"
    target.write_text("previous success")
    requests = []

    def response(url, **kwargs):
        requests.append((url, kwargs))
        if url.endswith("/sessions"):
            return {
                "session_id": "private-session-value",
                "host_id": "testflight-staging-mac-mini",
                "mode": "structured",
            }
        if "/messages?" in url:
            return {
                "messages": [
                    {
                        "session_id": "private-session-value",
                        "seq": 1,
                        "type": "assistant_output",
                        "role": role,
                        "text": text,
                    }
                ],
                "max_seq": 1,
            }
        return healthy_http(url, **kwargs)

    monkeypatch.setattr(stage, "http", response)
    if success:
        stage.probe(root, SHA, attempts=2)
        attestation = json.loads(target.read_text())
        assert set(attestation) == {
            "source_sha",
            "host_id",
            "completed_at",
            "session_id_sha256",
        }
        assert (
            attestation["session_id_sha256"]
            == hashlib.sha256(b"private-session-value").hexdigest()
        )
        assert attestation["source_sha"] == SHA
        assert (
            not {"token", "prompt", "reply", "session_id", "endpoint"}
            & attestation.keys()
        )
        assert "private-session-value" not in target.read_text()
        assert "http" not in target.read_text()
        assert target.stat().st_mode & 0o777 == 0o600
    else:
        with pytest.raises(stage.StageError):
            stage.probe(root, SHA, attempts=2)
        assert target.read_text() == "previous success"
    created = next(
        kwargs["payload"] for url, kwargs in requests if url.endswith("/sessions")
    )
    assert created["cwd"] == str(root / "workspace")
    assert created["mode"] == "structured"
    assert created["prompt"] == "Reply with exactly DROVER_TESTFLIGHT_STAGE_OK"
    assert requests[-1][0].endswith("/terminate")
    assert not list(root.glob("*.tmp"))


@pytest.mark.parametrize(
    "port,body",
    [
        (17080, b"ok\n"),
        (
            17081,
            b'{"ok": true, "host_id": "testflight-staging-mac-mini", "active_sessions": 0}',
        ),
    ],
)
def test_http_understands_both_real_health_formats(monkeypatch, port, body):
    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(body)

    monkeypatch.setattr(stage, "build_opener", lambda *args: Opener())
    stage.http(f"http://127.0.0.1:{port}/healthz")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:7080/healthz",
        "http://127.0.0.1:17082/healthz",
        "http://example.test:17080/healthz",
    ],
)
def test_http_rejects_other_ports_and_remote_hosts(url):
    with pytest.raises(stage.StageError):
        stage.http(url)


def test_http_rejects_false_readiness_even_with_http_200(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(b'{"ready": false}')

    monkeypatch.setattr(stage, "build_opener", lambda *args: Opener())
    with pytest.raises(stage.StageError):
        stage.http("http://127.0.0.1:17080/readyz")


def test_symlink_state_path_never_writes_outside_root(runtime):
    root = prepare(runtime)
    outside = runtime[1] / "outside"
    outside.mkdir()
    (root / "state/incoming").rmdir()
    (root / "state/incoming").symlink_to(outside, target_is_directory=True)
    with pytest.raises(stage.StageError):
        stage.activate(root, SHA)
    assert list(outside.iterdir()) == []


def test_activation_rejects_modified_staged_checkout(runtime, monkeypatch):
    root = ready_root(runtime, monkeypatch)
    original = stage.subprocess.run

    def dirty(argv, **kwargs):
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, " M source.py\n", "")
        return original(argv, **kwargs)

    monkeypatch.setattr(stage.subprocess, "run", dirty)
    runtime[2].clear()
    with pytest.raises(stage.StageError):
        stage.activate(root, SHA)
    assert not any(argv[0] == "launchctl" for argv, _ in runtime[2])


def test_disposable_cli_prepare_activate_rollback(runtime, monkeypatch):
    root, repo, _ = runtime
    assert (
        stage.main(
            [
                "prepare",
                "--repository",
                str(repo),
                "--root",
                str(root),
                "--sha",
                SHA,
                "--public-url",
                "https://stage.example.test",
            ]
        )
        == 0
    )
    token = root / "home/.drover/api_token"
    token.write_text("isolated-test-token")
    token.chmod(0o600)
    monkeypatch.setattr(stage, "http", healthy_http)
    assert stage.main(["activate", "--root", str(root), "--sha", SHA]) == 0
    prepare(runtime, OLDER)

    def older_identity(url, **kwargs):
        payload = healthy_http(url, **kwargs)
        if url.endswith("/release-identity"):
            payload["source_sha"] = OLDER
        return payload

    monkeypatch.setattr(stage, "http", older_identity)
    assert stage.main(["rollback", "--root", str(root), "--sha", OLDER]) == 0
    assert json.loads((root / "active-release.json").read_text())["source_sha"] == OLDER
    for path in (root / "launchd").glob("*.plist"):
        assert OLDER in path.read_text()
        assert "isolated-test-token" not in path.read_text()
        assert "com.drover.server" not in path.read_text()


def test_probe_does_not_attest_thinking_output(runtime, monkeypatch):
    root = ready_root(runtime, monkeypatch)

    def response(url, **kwargs):
        if url.endswith("/sessions"):
            return {
                "session_id": "private-probe",
                "host_id": "testflight-staging-mac-mini",
                "mode": "structured",
            }
        if "/messages?" in url:
            return {
                "messages": [
                    {
                        "session_id": "private-probe",
                        "seq": 1,
                        "type": "assistant_output",
                        "role": "assistant",
                        "text": "DROVER_TESTFLIGHT_STAGE_OK",
                        "payload": {"thinking": True},
                    }
                ],
                "max_seq": 1,
            }
        return healthy_http(url, **kwargs)

    monkeypatch.setattr(stage, "http", response)
    with pytest.raises(stage.StageError):
        stage.probe(root, SHA, attempts=1)
    assert not (root / "staging-probe.json").exists()


def test_atomic_failure_preserves_attestation_and_removes_temporary(
    runtime, monkeypatch
):
    root = prepare(runtime)
    path = root / "staging-probe.json"
    path.write_text("previous")

    def fail(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(stage.os, "replace", fail)
    with pytest.raises(OSError):
        stage.write_json(root, "staging-probe.json", {"source_sha": SHA})
    assert path.read_text() == "previous"
    assert not list(root.glob(".stage-*.tmp"))


def test_prepare_rejects_symlink_virtualenv_before_install(runtime):
    root = prepare(runtime)
    (root / "worktrees" / SHA / ".venv").symlink_to(
        runtime[1], target_is_directory=True
    )
    runtime[2].clear()
    with pytest.raises(stage.StageError):
        prepare(runtime)
    assert not any(argv[:2] == ["uv", "sync"] for argv, _ in runtime[2])


def test_activation_rejects_relative_apns_escape(runtime, monkeypatch):
    root = ready_root(runtime, monkeypatch)
    config = root / "home/.drover/config.toml"
    config.write_text(
        config.read_text() + '\n[apns]\nkey_path = "../../personal/key.p8"\n'
    )
    runtime[2].clear()
    with pytest.raises(stage.StageError):
        stage.activate(root, SHA)
    assert not any(argv[0] == "launchctl" for argv, _ in runtime[2])


def test_probe_rejects_wrong_session_host_and_cleans_up(runtime, monkeypatch):
    root = ready_root(runtime, monkeypatch)
    calls = []

    def response(url, **kwargs):
        calls.append(url)
        if url.endswith("/sessions"):
            return {
                "session_id": "private-probe",
                "host_id": "different-host",
                "mode": "structured",
            }
        return healthy_http(url, **kwargs)

    monkeypatch.setattr(stage, "http", response)
    with pytest.raises(stage.StageError):
        stage.probe(root, SHA, attempts=1)
    assert calls[-1].endswith("/terminate")
    assert not any("/messages?" in url for url in calls)
    assert not (root / "staging-probe.json").exists()
