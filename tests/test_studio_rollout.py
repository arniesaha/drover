"""Unit contracts for the Studio rollout script; no real launchctl is invoked."""

from __future__ import annotations

import importlib.util
import json
import plistlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "studio_rollout.py"
SPEC = importlib.util.spec_from_file_location("studio_rollout", SCRIPT)
assert SPEC and SPEC.loader
ROLLOUT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ROLLOUT
SPEC.loader.exec_module(ROLLOUT)


def test_environment_diff_reports_missing_without_exposing_values():
    result = ROLLOUT.environment_diff(
        {"DROVER_LAKE_DSN", "DROVER_WORK_COORDINATOR_CONTINUITY_SECRET"},
        {"DROVER_LAKE_DSN": "candidate"},
        {
            "DROVER_LAKE_DSN": "good",
            "DROVER_WORK_COORDINATOR_CONTINUITY_SECRET": "secret",
        },
        {
            "DROVER_LAKE_DSN": "running",
            "DROVER_WORK_COORDINATOR_CONTINUITY_SECRET": "secret",
        },
    )

    assert result["missing_required"] == {
        "DROVER_WORK_COORDINATOR_CONTINUITY_SECRET": [0]
    }
    assert "candidate" not in repr(result)
    assert result["variables"]["DROVER_LAKE_DSN"]["changed"] is True


def test_launchctl_environment_accepts_print_layout():
    assert ROLLOUT.launchctl_environment("""
        environment = {
            DROVER_LAKE_DSN => postgresql://redacted
            PATH => /usr/bin
        }
    """) == {"DROVER_LAKE_DSN": "postgresql://redacted", "PATH": "/usr/bin"}


def _write_wrapped_server_plist(path: Path) -> Path:
    env = path.parent / "server.env"
    env.write_text(
        'DROVER_LAKE_DSN="postgresql://lake"\n'
        'DROVER_WORK_QUEUE_GITHUB_TOKEN="queue-token"\n'
        'TOKEN="a\\$b"\n'
    )
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.drover.server",
                "EnvironmentVariables": {
                    "HOME": "/Users/studio",
                    "PATH": "/usr/bin:/bin",
                },
                "ProgramArguments": [
                    "/bin/sh",
                    "-c",
                    'set -a; . "$1" || exit $?; shift; exec "$@"',
                    "drover-service-env",
                    str(env),
                    "/runtime/current/bin/drover-server",
                    "--config",
                    "/Users/studio/.drover/config.toml",
                    "serve",
                ],
            }
        )
    )
    return env


def test_load_plist_keeps_launchd_and_effective_environment_separate(tmp_path: Path):
    plist = tmp_path / "com.drover.server.plist"
    _write_wrapped_server_plist(plist)

    view = ROLLOUT.load_plist(plist)
    assert view.launchd_environment == {
        "HOME": "/Users/studio",
        "PATH": "/usr/bin:/bin",
    }
    assert view.environment == {
        "HOME": "/Users/studio",
        "PATH": "/usr/bin:/bin",
        "DROVER_LAKE_DSN": "postgresql://lake",
        "DROVER_WORK_QUEUE_GITHUB_TOKEN": "queue-token",
        "TOKEN": "a$b",
    }


def test_running_plist_matches_live_wrapper_uses_launchd_environment_only(
    tmp_path: Path,
):
    plist = tmp_path / "com.drover.server.plist"
    _write_wrapped_server_plist(plist)
    view = ROLLOUT.load_plist(plist)
    printed = "\n".join(
        [
            f"path = {plist.resolve()}",
            "program = /bin/sh",
            "  HOME => /Users/studio",
            "  PATH => /usr/bin:/bin",
        ]
    )

    assert ROLLOUT.running_plist_matches(view, printed) is True
    assert (
        ROLLOUT.running_plist_matches(
            view, printed.replace("PATH => /usr/bin:/bin", "PATH => /unexpected")
        )
        is False
    )


def test_invalid_env_file_is_refused_without_shell_execution(tmp_path: Path):
    path = tmp_path / "server.env"
    path.write_text("DROVER_LAKE_DSN=$(bad-command)\n")
    with pytest.raises(ROLLOUT.RolloutError, match="environment_file_invalid"):
        ROLLOUT._read_env_file(path)


def test_rollout_token_uses_existing_local_client_credential(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('[auth]\nenabled = true\napi_token = ""\n')
    (tmp_path / "server.env").write_text('DROVER_API_TOKEN="from-server-env"\n')
    (tmp_path / "api_token").write_text("from-token-file\n")

    assert ROLLOUT.rollout_token(config, tmp_path) == "from-server-env"


def test_rollout_token_uses_api_token_file_and_override(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[auth]\nenabled = true\napi_token = ""\n')
    (tmp_path / "server.env").write_text('DROVER_CONTROL_DSN="postgresql://private"\n')
    (tmp_path / "api_token").write_text("from-token-file\n")
    monkeypatch.setenv("ROLLOUT_TEST_TOKEN", "override-token")

    assert ROLLOUT.rollout_token(config, tmp_path) == "from-token-file"
    assert (
        ROLLOUT.rollout_token(config, tmp_path, override_env="ROLLOUT_TEST_TOKEN")
        == "override-token"
    )


def test_last_good_snapshot_is_history_backed_and_pruned(tmp_path: Path):
    plists = tmp_path / "plists"
    plists.mkdir()
    for label in ROLLOUT.LABELS:
        (plists / f"{label}.plist").write_text(label)
    root = tmp_path / "rollout"
    now = datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc)

    for second in range(6):
        ROLLOUT.record_last_good(plists, root, now=now.replace(second=second))

    history = sorted((root / "history").iterdir())
    assert len(history) == 5
    assert (root / "last-good").is_symlink()
    assert (
        root / "last-good" / "com.drover.server.plist"
    ).read_text() == "com.drover.server"


def test_runtime_selection_detects_harnessd_repoint(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "target").mkdir()
    (runtime / "other").mkdir()
    (runtime / "current").symlink_to("target")

    assert ROLLOUT.runtime_points_to(runtime / "current", runtime / "target") is True
    (runtime / "current").unlink()
    (runtime / "current").symlink_to("other")
    assert ROLLOUT.runtime_points_to(runtime / "current", runtime / "target") is False


def _write_live_plist(path: Path, label: str, marker: str) -> None:
    environment = {"HOME": "/Users/studio", "PATH": "/usr/bin:/bin"}
    if label == "com.drover.server":
        environment.update(
            {
                "DROVER_LAKE_DSN": f"postgresql://lake-{marker}",
                "DROVER_WORK_COORDINATOR_CONTINUITY_SECRET": f"secret-{marker}",
            }
        )
        arguments = ["/runtime/current/bin/drover-server", "run"]
    else:
        arguments = [
            "/runtime/current/bin/drover-harnessd",
            "--host-id",
            marker,
            "--central-url",
            "https://studio.ts.net",
            "--relay",
        ]
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "EnvironmentVariables": environment,
                "ProgramArguments": arguments,
            }
        )
    )


def _required_by_label() -> dict[str, set[str]]:
    return {
        "com.drover.server": {
            "DROVER_LAKE_DSN",
            "DROVER_WORK_COORDINATOR_CONTINUITY_SECRET",
        },
        "com.drover.harnessd": set(),
    }


def test_bootstrap_last_good_accepts_server_env_and_minimal_harnessd(tmp_path: Path):
    plists = tmp_path / "plists"
    plists.mkdir()
    for label in ROLLOUT.LABELS:
        _write_live_plist(plists / f"{label}.plist", label, "current")

    running = {
        label: ROLLOUT.load_plist(plists / f"{label}.plist").environment
        for label in ROLLOUT.LABELS
    }
    snapshot = ROLLOUT.bootstrap_last_good(
        plists, tmp_path / "rollout", _required_by_label(), running
    )

    assert snapshot is not None
    assert (snapshot / "com.drover.harnessd.plist").is_file()


def test_bootstrap_last_good_refuses_server_missing_required_dsn(tmp_path: Path):
    plists = tmp_path / "plists"
    plists.mkdir()
    for label in ROLLOUT.LABELS:
        _write_live_plist(plists / f"{label}.plist", label, "current")
    server = plists / "com.drover.server.plist"
    payload = plistlib.loads(server.read_bytes())
    del payload["EnvironmentVariables"]["DROVER_LAKE_DSN"]
    server.write_bytes(plistlib.dumps(payload))

    with pytest.raises(ROLLOUT.RolloutError, match="required_environment_missing"):
        ROLLOUT.bootstrap_last_good(
            plists,
            tmp_path / "rollout",
            _required_by_label(),
            {
                label: ROLLOUT.load_plist(plists / f"{label}.plist").environment
                for label in ROLLOUT.LABELS
            },
        )


def _write_plist(path: Path, label: str, marker: str) -> None:
    _write_live_plist(path, label, marker)


def _main_fixture(tmp_path: Path, monkeypatch, *, changed: bool = True):
    home = tmp_path / "home"
    runtime = home / "runtime"
    old, target = runtime / "old", runtime / "target"
    (old / "bin").mkdir(parents=True)
    (target / "bin").mkdir(parents=True)
    (runtime / "current").symlink_to("old")
    plist_dir = tmp_path / "plists"
    candidate_dir = tmp_path / "candidate"
    last_good = home / "rollout" / "last-good"
    for directory, marker in (
        (plist_dir, "old"),
        (candidate_dir, "new" if changed else "old"),
        (last_good, "old"),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        for label in ROLLOUT.LABELS:
            _write_plist(directory / f"{label}.plist", label, marker)
    config = home / "config.toml"
    config.write_text(
        '[control_store]\nbackend = "postgres"\ndsn_env = "DROVER_LAKE_DSN"\n'
    )
    calls = []

    def fake_run(argv, *, dry_run, actions):
        calls.append(argv)
        if argv[:2] == ["launchctl", "print"]:
            label = argv[-1].rsplit("/", 1)[-1]
            path = plist_dir / f"{label}.plist"
            view = ROLLOUT.load_plist(path)
            return "\n".join(
                [
                    f"path = {path}",
                    f"program = {view.arguments[0]}",
                    *[
                        f"  {name} => {value}"
                        for name, value in view.environment.items()
                    ],
                ]
            )
        return ""

    monkeypatch.setattr(ROLLOUT, "_run", fake_run)
    monkeypatch.setattr(
        ROLLOUT,
        "required_environment",
        lambda _config, label: _required_by_label()[label],
    )
    monkeypatch.setattr(ROLLOUT, "runtime_preflight", lambda *_: {})
    monkeypatch.setattr(ROLLOUT, "rollout_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(
        ROLLOUT,
        "wait_for_work_queue",
        lambda *_args, **_kwargs: (True, {"active": 0, "stuck_intents": []}),
    )
    monkeypatch.setattr(ROLLOUT, "api_json", lambda *_: (True, {"hosts": []}))
    monkeypatch.setattr(ROLLOUT, "service_runs_target", lambda *_: True)
    monkeypatch.setattr(ROLLOUT, "runtime_points_to", lambda *_: True)
    monkeypatch.setattr(
        ROLLOUT,
        "record_last_good",
        lambda *_args, **_kwargs: home / "rollout/history/test",
    )
    args = [
        "target",
        "--drover-home",
        str(home),
        "--plist-dir",
        str(plist_dir),
        "--candidate-plists",
        str(candidate_dir),
        "--last-good-plists",
        str(last_good),
        "--config",
        str(config),
        "--tailscale-url",
        "https://studio.ts.net",
    ]
    return args, runtime, old, target, plist_dir, calls


def test_main_rolls_back_unexpected_exception_with_plists_and_stop_clear(
    tmp_path, monkeypatch
):
    args, runtime, old, _target, plists, calls = _main_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        ROLLOUT,
        "wait_for_health",
        lambda *_: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert ROLLOUT.main(args) == 1
    assert (runtime / "current").resolve() == old
    assert "old" in (plists / "com.drover.server.plist").read_text()
    bootouts = [
        command
        for command in calls
        if command
        == [
            "launchctl",
            "bootout",
            f"gui/{ROLLOUT.os.getuid()}/com.drover.server",
        ]
    ]
    bootstraps = [
        command
        for command in calls
        if command
        == [
            "launchctl",
            "bootstrap",
            f"gui/{ROLLOUT.os.getuid()}",
            str(plists / "com.drover.server.plist"),
        ]
    ]
    assert len(bootouts) == 2  # Forward reload and rollback reload.
    assert len(bootstraps) == 2
    assert any(command[-2:] == ["planned-stop", "complete"] for command in calls)


def test_main_preserves_backup_when_rollback_raises(tmp_path, monkeypatch, capsys):
    args, runtime, _old, _target, _plists, _calls = _main_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        ROLLOUT, "wait_for_health", lambda *_: (False, {"hub_loopback": {"ok": False}})
    )

    def rollback_raises(**_kwargs):
        raise RuntimeError("rollback failed")

    monkeypatch.setattr(ROLLOUT, "_rollback", rollback_raises)

    assert ROLLOUT.main(args) == 1
    report = json.loads(capsys.readouterr().out)
    preserved = Path(report["rollback"]["preserved_snapshot"])
    assert report["rollback"]["restored"] == "incomplete"
    assert preserved.parent == runtime.parent / "rollout" / "failed"
    assert (
        ROLLOUT.load_plist(preserved / "com.drover.server.plist").environment[
            "DROVER_LAKE_DSN"
        ]
        == "postgresql://lake-old"
    )


def test_main_refuses_stale_rollout_link_before_copy(tmp_path, monkeypatch):
    args, runtime, _old, _target, plists, calls = _main_fixture(tmp_path, monkeypatch)
    (runtime / ".current.rollout").symlink_to("target")

    assert ROLLOUT.main(args) == 1
    assert "old" in (plists / "com.drover.server.plist").read_text()
    assert not any("planned-stop" in command for command in calls)


def test_main_uses_kickstart_when_plists_are_unchanged(tmp_path, monkeypatch):
    args, _runtime, _old, _target, _plists, calls = _main_fixture(
        tmp_path, monkeypatch, changed=False
    )
    monkeypatch.setattr(ROLLOUT, "wait_for_health", lambda *_: (True, {}))

    assert ROLLOUT.main(args) == 0
    assert [
        "launchctl",
        "kickstart",
        "-k",
        f"gui/{ROLLOUT.os.getuid()}/com.drover.server",
    ] in calls
    assert not any(command[1:2] == ["bootout"] for command in calls)


def test_dry_run_refuses_running_plist_mismatch(tmp_path, monkeypatch):
    args, _runtime, _old, _target, _plists, calls = _main_fixture(tmp_path, monkeypatch)
    args.append("--dry-run")
    original_run = ROLLOUT._run

    def mismatched_run(argv, *, dry_run, actions):
        result = original_run(argv, dry_run=dry_run, actions=actions)
        if argv[:2] == ["launchctl", "print"]:
            return result.replace("PATH => /usr/bin:/bin", "PATH => /unexpected")
        return result

    monkeypatch.setattr(ROLLOUT, "_run", mismatched_run)

    assert ROLLOUT.main(args) == 1
    assert not any("planned-stop" in command for command in calls)


def test_main_health_failure_rolls_back_and_clears_planned_stop(tmp_path, monkeypatch):
    args, runtime, old, _target, plists, calls = _main_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        ROLLOUT, "wait_for_health", lambda *_: (False, {"hub_loopback": {"ok": False}})
    )

    assert ROLLOUT.main(args) == 1
    assert (runtime / "current").resolve() == old
    assert "old" in (plists / "com.drover.harnessd.plist").read_text()
    assert any(command[-2:] == ["planned-stop", "complete"] for command in calls)


def test_wait_for_health_reports_named_failed_check(monkeypatch):
    outcomes = iter([(False, {"error": "URLError"}), (True, {"status": 200})])
    clocks = iter([0, 2])
    monkeypatch.setattr(ROLLOUT, "health", lambda url: next(outcomes))
    monkeypatch.setattr(ROLLOUT.time, "monotonic", lambda: next(clocks))
    monkeypatch.setattr(ROLLOUT.time, "sleep", lambda _: None)
    ok, report = ROLLOUT.wait_for_health(
        {"hub": "http://hub", "harnessd": "http://harnessd"}, 1
    )
    assert ok is False
    assert report["hub"]["error"] == "URLError"


def test_relay_reconnect_and_work_queue_decisions_are_explicit():
    payload = {
        "hosts": [{"host_id": "nas", "connection_kind": "relay", "status": "online"}]
    }
    assert ROLLOUT.relay_hosts(payload) == {"nas"}
    assert ROLLOUT.relay_reconnected(payload, {"nas"}) == (True, [])
    assert ROLLOUT.work_queue_settled({"actions": []}) is True
    assert (
        ROLLOUT.work_queue_settled(
            {"actions": [{"kind": "launch_candidate"}], "execution": {"active": 0}}
        )
        is False
    )
    assert (
        ROLLOUT.work_queue_settled({"actions": [], "execution": {"active": 1}}) is False
    )


def test_optional_work_queue_report_tolerates_403_and_old_hub_shape(monkeypatch):
    monkeypatch.setattr(ROLLOUT, "api_json", lambda *_: (False, {"error": "HTTPError"}))
    report = ROLLOUT.optional_work_queue_report("http://hub/report", "device")
    assert report == {
        "ok": False,
        "report": {"error": "HTTPError"},
    }
    assert ROLLOUT.work_queue_settled({"actions": []}) is True
    assert ROLLOUT.wait_for_work_queue(
        lambda: {"active": 0, "stuck_intents": []}, 0, supplemental=lambda: report
    ) == (True, {"active": 0, "stuck_intents": [], "http_report": report})


def test_queue_settlement_keeps_only_fresh_sessionless_intents_active():
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    links = [
        {
            "id": "stale",
            "goal_id": "goal",
            "run_key": "stale-key",
            "session_id": None,
            "failure_count": 0,
            "created_at": now - timedelta(minutes=11),
            "updated_at": now - timedelta(minutes=11),
        },
        {
            "id": "systemic",
            "goal_id": "goal",
            "run_key": "systemic-key",
            "session_id": None,
            "failure_count": 3,
            "created_at": now,
            "updated_at": now,
        },
    ]

    settled = ROLLOUT.queue_settlement(
        links, orphan_intent_minutes=10, systemic_attempts={"goal": 3}, now=now
    )
    assert settled["active"] == 0
    assert {row["reason"] for row in settled["stuck_intents"]} == {
        "stale",
        "systemic",
    }
    fresh = ROLLOUT.queue_settlement(
        [
            {
                **links[0],
                "id": "fresh",
                "run_key": "fresh-key",
                "created_at": now,
                "updated_at": now,
            }
        ],
        orphan_intent_minutes=10,
        systemic_attempts={"goal": 3},
        now=now,
    )
    assert fresh["active"] == 1


def test_wait_for_work_queue_polls_until_local_links_are_gone(monkeypatch):
    reports = iter(
        [
            {"active": 1, "stuck_intents": []},
            {"active": 0, "stuck_intents": []},
        ]
    )
    monkeypatch.setattr(ROLLOUT.time, "monotonic", lambda: 0)
    monkeypatch.setattr(ROLLOUT.time, "sleep", lambda _: None)
    assert ROLLOUT.wait_for_work_queue(lambda: next(reports), 1) == (
        True,
        {"active": 0, "stuck_intents": []},
    )
