"""Activating a version by installing it into the venv that is already there.

On macOS the hub's launchd jobs cannot exec a venv on the external volume:
the process dies at interpreter startup with ``PermissionError: [Errno 1]
Operation not permitted`` reading its own ``pyvenv.cfg``. Errno 1 is EPERM,
which is TCC's signature rather than a Unix permission problem, and the grant
turned out to be keyed to the executable rather than to the launchd label. So
any activation that changes the executable identity loses the grant.

The answer on such a host is to leave the executable path and the interpreter
exactly where they are and install the new version *into* them. Linux has no
TCC and keeps the symlink flip, which is strictly safer, so this is opt-in.

These tests pin the two properties that make the opt-in safe: the symlink path
is untouched, and an in-place install that fails leaves the host on the version
it is currently running.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace

from drover.config import (
    ACTIVATION_IN_PLACE,
    ACTIVATION_SYMLINK,
    default_config,
    load_config,
)
from drover.server.harness.updater import (
    HostUpdater,
    resolve_activation,
    verify_after_restart,
)
from drover.server.runtime import RuntimeLayout
from drover.server.updates import LOCK_NAME, install_cached_into_venv


def _state(idle=True):
    return SimpleNamespace(
        structured=SimpleNamespace(session_ids=lambda: [], is_alive=lambda s: False),
        pty=SimpleNamespace(list_sessions=lambda: [] if idle else ["t1"]),
    )


def _beat(version="0.1.4"):
    return {
        "target_version": version,
        "artifact": {
            "url": f"https://x.test/drover-{version}-py3-none-any.whl",
            "sha256": "aa" * 32,
            "lock_url": "https://x.test/requirements.lock.txt",
            "lock_sha256": "bb" * 32,
        },
    }


def _installed(layout: RuntimeLayout, version: str, *, exit_code: int = 0) -> None:
    """A staged version tree: a runnable binary plus its cached artifacts."""
    binary = layout.version_dir(version) / "bin" / "drover-server"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    binary.chmod(0o755)
    _cached(layout, version)


def _cached(layout: RuntimeLayout, version: str) -> None:
    """Cache a version's artifacts the way a real install does: from a tempdir."""
    with tempfile.TemporaryDirectory() as work_dir:
        work = Path(work_dir)
        wheel = work / f"drover-{version}-py3-none-any.whl"
        wheel.write_bytes(b"wheel")
        lock = work / LOCK_NAME
        lock.write_text(f"drover=={version} --hash=sha256:aa\n", encoding="utf-8")
        layout.cache_artifact(version, wheel, lock)


def _venv(tmp_path: Path) -> Path:
    """The already-granted venv an in-place host installs into."""
    venv = tmp_path / "drover-venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (venv / "bin" / "python").chmod(0o755)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    return venv


class _Result:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _recording_runner(calls, returncode=0):
    def runner(command, **kwargs):
        calls.append(list(command))
        return _Result(returncode)

    return runner


def _in_place_cfg(venv: Path):
    return replace(
        default_config(),
        update_activation=ACTIVATION_IN_PLACE,
        update_in_place_venv=str(venv),
    )


def _updater(tmp_path, *, cfg, in_place_installer=None, idle=True, restarts=None):
    layout = RuntimeLayout(tmp_path / "home")
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")

    def install(lay, artifact, **kwargs):
        _installed(lay, artifact.version)
        return True

    kwargs = {}
    if in_place_installer is not None:
        kwargs["in_place_installer"] = in_place_installer
    return layout, HostUpdater(
        _state(idle=idle),
        layout,
        cfg,
        installer=install,
        restarter=restarts if restarts is not None else (lambda: None),
        **kwargs,
    )


def _refuse(*args, **kwargs):
    raise AssertionError("symlink mode must never install in place")


# --- the default path is untouched -------------------------------------------


def test_symlink_mode_activation_is_unchanged(tmp_path):
    """The regression guard: the default host still flips and restarts.

    An in-place installer that raises is the assertion. If the symlink path
    ever routes through it, this fails rather than quietly changing how every
    Linux host in the fleet activates.
    """
    restarts = []
    layout, updater = _updater(
        tmp_path,
        cfg=default_config(),
        in_place_installer=_refuse,
        restarts=lambda: restarts.append(1),
    )
    assert default_config().update_activation == ACTIVATION_SYMLINK

    updater.observe(_beat("0.1.4"))
    assert updater.maybe_activate() is True
    assert layout.active_version() == "0.1.4"
    assert layout.read_marker() == ("0.1.3", "0.1.4")
    assert restarts == [1]


# --- in-place activation ------------------------------------------------------


def test_in_place_activation_installs_into_the_configured_venv(tmp_path):
    """The whole point: the executable and the interpreter do not move."""
    venv = _venv(tmp_path)
    calls = []
    layout, updater = _updater(
        tmp_path,
        cfg=_in_place_cfg(venv),
        in_place_installer=partial(
            install_cached_into_venv, runner=_recording_runner(calls)
        ),
    )
    updater.observe(_beat("0.1.4"))
    assert updater.maybe_activate() is True

    flat = [" ".join(command) for command in calls]
    assert flat, "nothing was installed"
    python = str(venv / "bin" / "python")
    assert all(f"--python {python}" in line for line in flat)
    assert any("--require-hashes" in line for line in flat)
    assert any("--no-deps" in line for line in flat)
    # The cached artifacts, not a fresh download.
    cached = layout.cached_artifact("0.1.4")
    assert cached is not None
    assert any(str(cached.lock) in line for line in flat)
    assert any(str(cached.wheel) in line for line in flat)
    # A new venv is exactly what loses the TCC grant.
    assert not any("uv venv" in line for line in flat)


def test_in_place_activation_flips_the_record_symlink(tmp_path):
    """The symlink stops being the exec path but stays the record of truth.

    `active_version`, the hub's target comparison and `prune` all read it, so
    an in-place host that never flipped would reinstall the same version on
    every heartbeat and prune the wrong trees.
    """
    venv = _venv(tmp_path)
    restarts = []
    layout, updater = _updater(
        tmp_path,
        cfg=_in_place_cfg(venv),
        in_place_installer=partial(
            install_cached_into_venv, runner=_recording_runner([])
        ),
        restarts=lambda: restarts.append(1),
    )
    updater.observe(_beat("0.1.4"))
    assert updater.maybe_activate() is True

    assert layout.active_version() == "0.1.4"
    assert layout.read_marker() == ("0.1.3", "0.1.4")
    assert restarts == [1]
    assert updater.status()["update_blocked"] is False


def test_a_failed_in_place_install_leaves_the_host_where_it_was(tmp_path):
    """Half-updating a host is worse than not updating it."""
    venv = _venv(tmp_path)
    layout, updater = _updater(
        tmp_path,
        cfg=_in_place_cfg(venv),
        in_place_installer=partial(
            install_cached_into_venv, runner=_recording_runner([], returncode=1)
        ),
        restarts=lambda: (_ for _ in ()).throw(
            AssertionError("must not restart onto a failed install")
        ),
    )
    updater.observe(_beat("0.1.4"))

    assert updater.maybe_activate() is False
    assert layout.active_version() == "0.1.3"
    status = updater.status()
    assert status["update_blocked"] is True
    assert status["reason"] == "install_failed"
    assert status["blocked_version"] == "0.1.4"


def test_in_place_activation_without_a_cached_artifact_refuses(tmp_path):
    """A version installed before caching existed cannot be activated in place.

    Refusing is the answer rather than re-downloading: activation runs when
    the host has finally gone idle, and reaching for the network there turns
    a local operation into one that can hang.
    """
    venv = _venv(tmp_path)
    layout = RuntimeLayout(tmp_path / "home")
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")

    def install_without_cache(lay, artifact, **kwargs):
        binary = lay.version_dir(artifact.version) / "bin" / "drover-server"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        return True

    calls = []
    updater = HostUpdater(
        _state(),
        layout,
        _in_place_cfg(venv),
        installer=install_without_cache,
        restarter=lambda: None,
        in_place_installer=partial(
            install_cached_into_venv, runner=_recording_runner(calls)
        ),
    )
    updater.observe(_beat("0.1.4"))

    assert updater.maybe_activate() is False
    assert calls == [], "uv must not run without a verified artifact to install"
    assert layout.active_version() == "0.1.3"
    assert updater.status()["reason"] == "install_failed"


# --- every gate still runs, and in the same order -----------------------------


def test_a_busy_host_does_not_install_in_place(tmp_path):
    venv = _venv(tmp_path)
    layout, updater = _updater(
        tmp_path,
        cfg=_in_place_cfg(venv),
        in_place_installer=_refuse,
        idle=False,
    )
    updater.observe(_beat("0.1.4"))

    assert updater.maybe_activate() is False
    assert layout.active_version() == "0.1.3"
    assert layout.read_marker() is None, "no marker until we commit to activating"
    assert updater.status()["reason"] == "not_quiescent"


def test_a_version_failing_its_smoke_test_does_not_install_in_place(tmp_path):
    """The staged tree is still what proves the version can run at all."""
    venv = _venv(tmp_path)
    layout = RuntimeLayout(tmp_path / "home")
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")

    def install_broken(lay, artifact, **kwargs):
        _installed(lay, artifact.version, exit_code=1)
        return True

    updater = HostUpdater(
        _state(),
        layout,
        _in_place_cfg(venv),
        installer=install_broken,
        restarter=lambda: None,
        in_place_installer=_refuse,
    )
    updater.observe(_beat("0.1.4"))

    assert updater.maybe_activate() is False
    assert layout.active_version() == "0.1.3"
    assert updater.status()["reason"] == "smoke_test"


# --- rollback -----------------------------------------------------------------


def test_rollback_reinstalls_the_previous_wheel_in_place(tmp_path):
    """Flipping the record back would not undo anything on an in-place host.

    The venv still holds the version that could not register, so rolling back
    means installing the previous version's cached wheel over it.
    """
    venv = _venv(tmp_path)
    layout = RuntimeLayout(tmp_path / "home")
    _installed(layout, "0.1.3")
    _installed(layout, "0.1.4")
    layout.flip("0.1.4")
    layout.write_marker("0.1.3", "0.1.4")
    calls = []

    assert (
        verify_after_restart(
            layout,
            registered=False,
            activation=ACTIVATION_IN_PLACE,
            in_place_venv=str(venv),
            in_place_installer=partial(
                install_cached_into_venv, runner=_recording_runner(calls)
            ),
        )
        is False
    )

    flat = [" ".join(command) for command in calls]
    previous = layout.cached_artifact("0.1.3")
    assert previous is not None
    assert any(str(previous.wheel) in line for line in flat), flat
    assert all(str(venv / "bin" / "python") in line for line in flat)
    assert layout.active_version() == "0.1.3"
    assert layout.read_marker() is None, "must not roll back again on the next boot"


def test_rollback_with_no_previous_version_changes_nothing(tmp_path):
    """No runtime at all is worse than one that at least starts."""
    venv = _venv(tmp_path)
    layout = RuntimeLayout(tmp_path / "home")
    _installed(layout, "0.1.4")
    layout.flip("0.1.4")
    layout.write_marker("", "0.1.4")
    calls = []

    assert (
        verify_after_restart(
            layout,
            registered=False,
            activation=ACTIVATION_IN_PLACE,
            in_place_venv=str(venv),
            in_place_installer=partial(
                install_cached_into_venv, runner=_recording_runner(calls)
            ),
        )
        is False
    )

    assert calls == []
    assert layout.active_version() == "0.1.4"
    assert layout.read_marker() is None


def test_rollback_that_cannot_reinstall_does_not_lie_about_what_is_active(tmp_path):
    """The record symlink has to keep matching what the venv actually holds."""
    venv = _venv(tmp_path)
    layout = RuntimeLayout(tmp_path / "home")
    _installed(layout, "0.1.3")
    _installed(layout, "0.1.4")
    layout.flip("0.1.4")
    layout.write_marker("0.1.3", "0.1.4")

    assert (
        verify_after_restart(
            layout,
            registered=False,
            activation=ACTIVATION_IN_PLACE,
            in_place_venv=str(venv),
            in_place_installer=partial(
                install_cached_into_venv, runner=_recording_runner([], returncode=1)
            ),
        )
        is False
    )

    assert layout.active_version() == "0.1.4"
    assert layout.read_marker() is None


def test_rollback_in_symlink_mode_still_only_flips(tmp_path):
    layout = RuntimeLayout(tmp_path / "home")
    _installed(layout, "0.1.3")
    _installed(layout, "0.1.4")
    layout.flip("0.1.4")
    layout.write_marker("0.1.3", "0.1.4")

    assert (
        verify_after_restart(layout, registered=False, in_place_installer=_refuse)
        is False
    )
    assert layout.active_version() == "0.1.3"
    assert layout.read_marker() is None


# --- configuration ------------------------------------------------------------


def test_activation_defaults_to_symlink():
    cfg = default_config()
    assert cfg.update_activation == ACTIVATION_SYMLINK
    assert cfg.update_in_place_venv == ""


def test_an_unrecognised_activation_mode_falls_back_to_symlink(tmp_path):
    """Never fail the daemon over a config typo."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[update]\nactivation = "in-place"\nin_place_venv = "/somewhere"\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.update_activation == ACTIVATION_SYMLINK

    layout, updater = _updater(tmp_path, cfg=cfg, in_place_installer=_refuse)
    updater.observe(_beat("0.1.4"))
    assert updater.maybe_activate() is True
    assert layout.active_version() == "0.1.4"


def test_in_place_without_a_venv_path_falls_back_to_symlink(tmp_path):
    """The venv must be explicit; guessing one is how you brick the wrong host."""
    cfg = replace(default_config(), update_activation=ACTIVATION_IN_PLACE)
    layout, updater = _updater(tmp_path, cfg=cfg, in_place_installer=_refuse)
    updater.observe(_beat("0.1.4"))
    assert updater.maybe_activate() is True
    assert layout.active_version() == "0.1.4"


def test_the_venv_path_is_expanded_once(tmp_path, monkeypatch):
    """launchd and systemd do not run a shell, so `~` would stay literal."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = replace(
        default_config(),
        update_activation=ACTIVATION_IN_PLACE,
        update_in_place_venv="~/.drover-venv",
    )
    assert resolve_activation(cfg) == (
        ACTIVATION_IN_PLACE,
        str(tmp_path / ".drover-venv"),
    )


def test_activation_mode_round_trips_through_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[update]\nactivation = "in_place"\nin_place_venv = "~/.drover-venv"\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.update_activation == ACTIVATION_IN_PLACE
    assert cfg.update_in_place_venv == "~/.drover-venv"
