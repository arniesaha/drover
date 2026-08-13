"""Install eagerly, activate only when idle, roll back if the new one dies.

The watchdog is the reason this is safe to run on a machine you can only
reach awkwardly: a version that cannot register with the hub within the
deadline undoes its own flip, so a bad release costs ninety seconds rather
than physical access.
"""

from __future__ import annotations

from types import SimpleNamespace

from drover.config import default_config
from drover.server.harness.updater import HostUpdater, verify_after_restart
from drover.server.runtime import RuntimeLayout


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


def _installed(layout, version):
    binary = layout.version_dir(version) / "bin" / "drover-server"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)


def _updater(tmp_path, *, idle=True, installs=True, restarts=None, cfg=None):
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")

    def install(lay, artifact, **kwargs):
        if not installs:
            return False
        _installed(lay, artifact.version)
        return True

    return layout, HostUpdater(
        _state(idle=idle),
        layout,
        cfg or default_config(),
        installer=install,
        restarter=restarts if restarts is not None else (lambda: None),
    )


def test_a_matching_version_is_a_no_op(tmp_path):
    layout, updater = _updater(tmp_path)
    updater.observe(_beat("0.1.3"))
    assert updater.maybe_activate() is False
    assert layout.active_version() == "0.1.3"


def test_an_older_target_is_ignored(tmp_path):
    """Downgrade is never automatic, even if the hub asks."""
    layout, updater = _updater(tmp_path)
    updater.observe(_beat("0.1.1"))
    assert updater.maybe_activate() is False
    assert layout.active_version() == "0.1.3"


def test_a_heartbeat_with_no_target_is_a_no_op(tmp_path):
    layout, updater = _updater(tmp_path)
    updater.observe({})
    updater.observe({"host": {}})
    assert updater.maybe_activate() is False


def test_a_newer_version_installs_but_does_not_flip_yet(tmp_path):
    layout, updater = _updater(tmp_path)
    updater.observe(_beat("0.1.4"))
    assert "0.1.4" in layout.installed_versions()
    assert layout.active_version() == "0.1.3", "installed is not activated"


def test_activation_happens_once_idle(tmp_path):
    layout, updater = _updater(tmp_path)
    updater.observe(_beat("0.1.4"))
    assert updater.maybe_activate() is True
    assert layout.active_version() == "0.1.4"
    assert layout.read_marker() == ("0.1.3", "0.1.4")


def test_a_busy_host_does_not_activate(tmp_path):
    layout, updater = _updater(tmp_path, idle=False)
    updater.observe(_beat("0.1.4"))
    assert updater.maybe_activate() is False
    assert layout.active_version() == "0.1.3"
    assert layout.read_marker() is None, "no marker until we actually flip"


def test_a_busy_host_reports_that_it_is_blocked(tmp_path):
    _, updater = _updater(tmp_path, idle=False)
    updater.observe(_beat("0.1.4"))
    updater.maybe_activate()
    assert updater.status()["update_blocked"] is True
    assert updater.status()["pending_version"] == "0.1.4"


def test_a_failed_install_never_activates(tmp_path):
    layout, updater = _updater(tmp_path, installs=False)
    updater.observe(_beat("0.1.4"))
    assert updater.maybe_activate() is False
    assert layout.active_version() == "0.1.3"


def test_installing_is_not_retried_on_every_heartbeat(tmp_path):
    """A host can wait hours for quiesce; it must not re-download all of it."""
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")
    calls = []

    def install(lay, artifact, **kwargs):
        calls.append(artifact.version)
        _installed(lay, artifact.version)
        return True

    updater = HostUpdater(
        _state(idle=False),
        layout,
        default_config(),
        installer=install,
        restarter=lambda: None,
    )
    for _ in range(5):
        updater.observe(_beat("0.1.4"))
    assert calls == ["0.1.4"]


def test_activation_calls_the_restarter(tmp_path):
    calls = []
    layout, updater = _updater(tmp_path, restarts=lambda: calls.append(1))
    updater.observe(_beat("0.1.4"))
    updater.maybe_activate()
    assert calls == [1]


def test_a_version_that_fails_its_smoke_test_is_not_activated(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")

    def install_broken(lay, artifact, **kwargs):
        binary = lay.version_dir(artifact.version) / "bin" / "drover-server"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        binary.chmod(0o755)
        return True

    updater = HostUpdater(
        _state(),
        layout,
        default_config(),
        installer=install_broken,
        restarter=lambda: None,
    )
    updater.observe(_beat("0.1.4"))
    assert updater.maybe_activate() is False
    assert layout.active_version() == "0.1.3"


# --- watchdog -----------------------------------------------------------------


def test_watchdog_clears_the_marker_when_registration_succeeds(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.3")
    _installed(layout, "0.1.4")
    layout.flip("0.1.4")
    layout.write_marker("0.1.3", "0.1.4")

    assert verify_after_restart(layout, registered=True) is True
    assert layout.read_marker() is None
    assert layout.active_version() == "0.1.4"


def test_watchdog_rolls_back_when_registration_fails(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.3")
    _installed(layout, "0.1.4")
    layout.flip("0.1.4")
    layout.write_marker("0.1.3", "0.1.4")

    assert verify_after_restart(layout, registered=False) is False
    assert layout.active_version() == "0.1.3", "a host must not brick itself"
    assert layout.read_marker() is None, "and must not roll back again on next boot"


def test_watchdog_with_no_marker_does_nothing(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")
    assert verify_after_restart(layout, registered=False) is True
    assert layout.active_version() == "0.1.3"


def test_watchdog_with_no_previous_version_does_not_flip_to_nothing(tmp_path):
    """An empty previous would symlink current at "" and leave no runtime."""
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.4")
    layout.flip("0.1.4")
    layout.write_marker("", "0.1.4")

    assert verify_after_restart(layout, registered=False) is False
    assert layout.active_version() == "0.1.4"
    assert layout.read_marker() is None
