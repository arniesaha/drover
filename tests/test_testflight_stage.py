"""Local deployment boundaries; subprocesses and HTTP never reach a real host."""

import hashlib
import io
import json
import plistlib
import subprocess
import tomllib
from pathlib import Path

import pytest
from test_harness_update_wiring import _run_harnessd_capturing_state
from testflight import stage

from drover.config import load_config
from drover.server.harness import updater

SHA = "a" * 40
OLDER = "b" * 40


def test_cloudflared_example_exposes_only_the_staging_origin():
    """A public tunnel must terminate at the staging HTTP listener only."""
    template = (
        Path(__file__).parents[1] / "deploy/testflight-staging/cloudflared.yml.example"
    )
    text = template.read_text()

    entries = [line.strip().removeprefix("- ").strip() for line in text.splitlines()]
    hostnames = [entry for entry in entries if entry.startswith("hostname:")]
    services = [entry for entry in entries if entry.startswith("service:")]

    assert hostnames == ["hostname: <staging-public-hostname>"]
    assert services == [
        "service: http://127.0.0.1:17080",
        "service: http_status:404",
    ]
    for forbidden in ["7081", "7077", "4317", "0.0.0.0"]:
        assert forbidden not in text


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
    if url.endswith("/terminate"):
        return {
            "session_id": url.split("/")[-2],
            "host_id": "testflight-staging-mac-mini",
            "terminated": True,
            "status": "terminated",
        }
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


def test_staging_effective_config_never_starts_updater(runtime, monkeypatch):
    root = prepare(runtime)
    cfg = load_config(root / "home/.drover/config.toml")
    assert cfg.update_enabled is False
    state = _run_harnessd_capturing_state(monkeypatch, root / "state", cfg)
    assert state.updater is None


def test_staging_updater_restarter_can_only_address_dedicated_label(
    runtime, monkeypatch
):
    root = prepare(runtime)
    for name in ("server", "harnessd"):
        label = f"com.drover.testflight-{name}"
        job = plistlib.loads((root / "launchd" / f"{label}.plist").read_bytes())
        assert job["EnvironmentVariables"]["XPC_SERVICE_NAME"] == label
        monkeypatch.setenv("XPC_SERVICE_NAME", label)
        monkeypatch.setattr(updater.sys, "platform", "darwin")
        updater.default_restarter()
        assert runtime[2][-1][0][-1].endswith("/" + label)


def test_activate_rejects_enabled_updater(runtime, monkeypatch):
    root = ready_root(runtime, monkeypatch)
    path = root / "home/.drover/config.toml"
    path.write_text(
        path.read_text().replace(
            "[update]\nenabled = false", "[update]\nenabled = true"
        )
    )
    with pytest.raises(stage.StageError):
        stage.activate(root, SHA)


@pytest.mark.parametrize(
    "relative",
    [
        "home/.claude",
        "home/.claude/projects",
        "home/.codex/auth.json",
        "home/.drover/model-catalog-scope.key",
        "logs/com.drover.testflight-server.stdout.log",
        "logs/com.drover.testflight-harnessd.stderr.log",
    ],
)
@pytest.mark.parametrize("action", ["prepare", "activate", "probe", "rollback"])
def test_lifecycle_rejects_implicit_state_and_log_symlink_escapes(
    runtime, monkeypatch, relative, action
):
    root = ready_root(runtime, monkeypatch)
    outside = runtime[1] / "private-data"
    outside.mkdir()
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)

    def unexpected_http(*args, **kwargs):
        pytest.fail("unsafe staging path reached the HTTP boundary")

    monkeypatch.setattr(stage, "http", unexpected_http)
    runtime[2].clear()
    with pytest.raises(stage.StageError):
        if action == "prepare":
            prepare(runtime)
        else:
            getattr(stage, action)(root, SHA)
    assert not any(argv[0] == "launchctl" for argv, _ in runtime[2])
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "termination",
    [
        {},
        {"terminated": False},
        {
            "terminated": True,
            "status": "running",
            "host_id": "testflight-staging-mac-mini",
            "session_id": "probe-test",
        },
        {
            "terminated": True,
            "status": "terminated",
            "host_id": "wrong-host",
            "session_id": "probe-test",
        },
        {
            "terminated": True,
            "status": "terminated",
            "host_id": "testflight-staging-mac-mini",
            "session_id": "stale-test",
        },
    ],
)
def test_probe_requires_exact_confirmed_termination(runtime, monkeypatch, termination):
    root = ready_root(runtime, monkeypatch)
    attestation = root / "staging-probe.json"
    attestation.write_text("previous")

    def response(url, **kwargs):
        if url.endswith("/sessions"):
            return {
                "session_id": "probe-test",
                "host_id": "testflight-staging-mac-mini",
                "mode": "structured",
            }
        if "/messages?" in url:
            return {
                "messages": [
                    {
                        "session_id": "probe-test",
                        "seq": 1,
                        "type": "assistant_output",
                        "role": "assistant",
                        "text": "DROVER_TESTFLIGHT_STAGE_OK",
                    }
                ]
            }
        if url.endswith("/terminate"):
            return termination
        return healthy_http(url, **kwargs)

    monkeypatch.setattr(stage, "http", response)
    with pytest.raises(stage.StageError):
        stage.probe(root, SHA, attempts=1)
    assert attestation.read_text() == "previous"


def _probing_http(termination, *, answers=True, harness_drops=False, terminate=None):
    """`harness_drops` wedges the staging daemon *after* the probe's own
    opening health check, which is when a session already exists to clean up."""
    health_checks = []

    def response(url, **kwargs):
        if url == "http://127.0.0.1:17081/healthz":
            health_checks.append(url)
            if harness_drops and len(health_checks) > 1:
                raise stage.StageError("loopback API request failed")
        if url.endswith("/sessions"):
            return {
                "session_id": "probe-test",
                "host_id": "testflight-staging-mac-mini",
                "mode": "structured",
            }
        if "/messages?" in url:
            return {
                "messages": [
                    {
                        "session_id": "probe-test",
                        "seq": 1,
                        "type": "assistant_output",
                        "role": "assistant",
                        "text": (
                            "DROVER_TESTFLIGHT_STAGE_OK"
                            if answers
                            else "something else"
                        ),
                    }
                ]
            }
        if url.endswith("/terminate"):
            if terminate is not None:
                return terminate()
            return termination
        return healthy_http(url, **kwargs)

    return response


def test_probe_accepts_a_session_the_hub_has_already_forgotten(runtime, monkeypatch):
    """The stale shape is cleanup succeeding, not failing.

    A hub that has lost the session answers session_id + status + stale, with
    no host_id and no `terminated` (see
    MetricsCollector._proxy_terminate_harness_session). Demanding the full
    shape threw away an otherwise good probe.
    """
    root = ready_root(runtime, monkeypatch)
    monkeypatch.setattr(
        stage,
        "http",
        _probing_http(
            {"session_id": "probe-test", "status": "terminated", "stale": True}
        ),
    )

    stage.probe(root, SHA, attempts=1)

    attested = json.loads((root / "staging-probe.json").read_text())
    assert attested["source_sha"] == SHA
    assert attested["host_id"] == "testflight-staging-mac-mini"


def test_probe_refuses_a_stale_answer_from_an_unreachable_host(runtime, monkeypatch):
    """The hub answers `stale` for 404 *and* for a host it could not reach.

    Only the first means the session is gone. If the staging daemon is not
    answering, the probe's agent may still be running there holding the
    staging API key, so this is not cleanup and must not attest.
    """
    root = ready_root(runtime, monkeypatch)
    attestation = root / "staging-probe.json"
    attestation.write_text("previous")
    monkeypatch.setattr(
        stage,
        "http",
        _probing_http(
            {"session_id": "probe-test", "status": "terminated", "stale": True},
            harness_drops=True,
        ),
    )

    with pytest.raises(stage.StageError):
        stage.probe(root, SHA, attempts=1)

    assert attestation.read_text() == "previous"


def test_probe_failure_survives_a_cleanup_that_cannot_be_delivered(
    runtime, monkeypatch
):
    """The terminate call runs in `finally` and can fail the same way.

    A wedged daemon breaks the probe and the cleanup together, so the raise
    from the cleanup path is exactly the one likeliest to replace the real
    reason the probe stopped.
    """
    root = ready_root(runtime, monkeypatch)

    def unreachable():
        raise stage.StageError("loopback API request failed")

    monkeypatch.setattr(
        stage, "http", _probing_http(None, answers=False, terminate=unreachable)
    )

    with pytest.raises(stage.StageError) as failure:
        stage.probe(root, SHA, attempts=1)

    assert "expected assistant response" in str(failure.value)
    assert not (root / "staging-probe.json").exists()


def test_probe_reports_a_cleanup_it_could_not_deliver_after_a_good_run(
    runtime, monkeypatch
):
    """A successful probe still may not attest if cleanup went unconfirmed."""
    root = ready_root(runtime, monkeypatch)

    def unreachable():
        raise stage.StageError("loopback API request failed")

    monkeypatch.setattr(stage, "http", _probing_http(None, terminate=unreachable))

    with pytest.raises(stage.StageError):
        stage.probe(root, SHA, attempts=1)

    assert not (root / "staging-probe.json").exists()


def test_probe_failure_survives_an_unconfirmed_cleanup(runtime, monkeypatch):
    """Raising from `finally` replaced the real reason the probe failed."""
    root = ready_root(runtime, monkeypatch)
    monkeypatch.setattr(stage, "http", _probing_http({}, answers=False))

    with pytest.raises(stage.StageError) as failure:
        stage.probe(root, SHA, attempts=1)

    assert "expected assistant response" in str(failure.value)
    assert not (root / "staging-probe.json").exists()


def test_prepare_requires_candidate_credential_boundary(runtime, monkeypatch):
    original = stage.subprocess.run

    def run(argv, **kwargs):
        if "STAGING_CREDENTIAL_BOUNDARY_VERSION" in " ".join(argv):
            return subprocess.CompletedProcess(argv, 1, "", "")
        return original(argv, **kwargs)

    monkeypatch.setattr(stage.subprocess, "run", run)
    with pytest.raises(stage.StageError):
        prepare(runtime)
    assert not (runtime[0] / "active-release.json").exists()


@pytest.mark.parametrize("escaping", [False, True])
def test_prepare_accepts_only_contained_uv_cache_output(runtime, monkeypatch, escaping):
    root, repository, _ = runtime
    original = stage.subprocess.run

    def sync_with_cache(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[:2] == ["uv", "sync"]:
            cache = Path(kwargs["env"]["UV_CACHE_DIR"])
            archive = cache / "archive-v0" / "archive-fixture"
            archive.mkdir(parents=True)
            (archive / "package.py").write_text("# disposable wheel content\n")
            wheel = cache / "wheels-v5" / "pypi" / "fixture"
            wheel.mkdir(parents=True)
            destination = repository if escaping else archive
            (wheel / "fixture-1.0-py3-none-any").symlink_to(
                destination, target_is_directory=True
            )
        return result

    monkeypatch.setattr(stage.subprocess, "run", sync_with_cache)
    if escaping:
        with pytest.raises(stage.StageError):
            prepare(runtime)
        assert not (root / "active-release.json").exists()
    else:
        prepare(runtime)
        assert (
            json.loads((root / "active-release.json").read_text())["source_sha"] == SHA
        )
        assert (root / "launchd/com.drover.testflight-server.plist").is_file()
        # Subsequent lifecycle validation accepts the same cache output.
        stage.release(root, SHA)


@pytest.mark.parametrize("destination", ["home", "state", "logs", "workspace"])
def test_cache_links_cannot_target_other_staging_trees(runtime, destination):
    root = prepare(runtime)
    (root / "cache/redirect").symlink_to(root / destination, target_is_directory=True)
    with pytest.raises(stage.StageError):
        stage.release(root, SHA)


def test_internal_cache_link_cannot_hide_descendant_escape(runtime):
    root = prepare(runtime)
    archive = root / "cache/uv/archive-v0/fixture"
    archive.mkdir(parents=True)
    (archive / "escape").symlink_to(runtime[1], target_is_directory=True)
    (root / "cache/alias").symlink_to(archive, target_is_directory=True)
    with pytest.raises(stage.StageError):
        stage.release(root, SHA)
