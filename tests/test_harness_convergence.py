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
    status = updater.status()
    assert status["update_blocked"] is True
    assert status["pending_version"] == "0.1.4"
    assert status["reason"] == "not_quiescent"
    assert status["observed_at"] is not None


def test_a_failed_install_never_activates(tmp_path):
    layout, updater = _updater(tmp_path, installs=False)
    updater.observe(_beat("0.1.4"))
    assert updater.maybe_activate() is False
    assert layout.active_version() == "0.1.3"
    status = updater.status()
    assert status["update_blocked"] is True
    assert status["reason"] == "install_failed"
    assert status["pending_version"] is None
    assert status["observed_at"] is not None


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
    status = updater.status()
    assert status["update_blocked"] is False
    assert status["reason"] is None


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
    status = updater.status()
    assert status["update_blocked"] is True
    assert status["reason"] == "smoke_test"
    assert status["pending_version"] == "0.1.4"
    assert status["observed_at"] is not None
    assert status["blocked_version"] == "0.1.4"


# --- what the hub is told about a refusal -------------------------------------


def _broken_installer(lay, artifact, **kwargs):
    """Installs a binary that exists but cannot state its own version."""
    binary = lay.version_dir(artifact.version) / "bin" / "drover-server"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    binary.chmod(0o755)
    return True


def test_a_retracted_release_clears_the_refusal_it_caused(tmp_path):
    """Pulling a bad release is the remediation; it must end the report.

    A fleet-wide smoke-test refusal is answered by yanking the release. If the
    host keeps reporting the refusal after the hub stops asking for it, the
    operator who just fixed the problem sees no change -- and because
    observed_at used to be restamped every beat, the stale refusal looked
    seconds old forever.
    """
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")
    updater = HostUpdater(
        _state(),
        layout,
        default_config(),
        installer=_broken_installer,
        restarter=lambda: None,
    )
    updater.observe(_beat("0.1.4"))
    assert updater.maybe_activate() is False
    assert updater.status()["reason"] == "smoke_test"

    # The operator pulls 0.1.4, so the hub publishes no target at all.
    updater.observe({})

    assert updater.status() == {
        "pending_version": None,
        "blocked_version": None,
        "update_blocked": False,
        "reason": None,
        "observed_at": None,
    }


def test_a_retracted_release_stops_the_smoke_test_subprocess(tmp_path):
    """No target means nothing to check, and a smoke test is a subprocess."""
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")
    smoke_calls = []
    real_smoke = layout.smoke_test

    def counting_smoke(version):
        smoke_calls.append(version)
        return real_smoke(version)

    layout.smoke_test = counting_smoke
    updater = HostUpdater(
        _state(),
        layout,
        default_config(),
        installer=_broken_installer,
        restarter=lambda: None,
    )
    updater.observe(_beat("0.1.4"))
    updater.maybe_activate()
    assert smoke_calls == ["0.1.4"]

    updater.observe({})
    for _ in range(3):
        assert updater.maybe_activate() is False
    assert smoke_calls == ["0.1.4"], "no target, so nothing to smoke test"


def test_a_refusal_clears_once_a_good_version_arrives(tmp_path):
    """The recovery half: a refusal must not outlive the version it was about."""
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")
    restarts = []

    def install(lay, artifact, **kwargs):
        if artifact.version == "0.1.4":
            return _broken_installer(lay, artifact, **kwargs)
        _installed(lay, artifact.version)
        return True

    updater = HostUpdater(
        _state(),
        layout,
        default_config(),
        installer=install,
        restarter=lambda: restarts.append(1),
    )
    updater.observe(_beat("0.1.4"))
    assert updater.maybe_activate() is False
    assert updater.status()["reason"] == "smoke_test"

    # 0.1.5 is cut with the fix in it.
    updater.observe(_beat("0.1.5"))
    assert updater.maybe_activate() is True

    assert layout.active_version() == "0.1.5"
    assert restarts == [1]
    assert updater.status() == {
        "pending_version": None,
        "blocked_version": None,
        "update_blocked": False,
        "reason": None,
        "observed_at": None,
    }


def test_observed_at_records_when_the_refusal_began_not_the_last_check(tmp_path):
    """Otherwise every blocked host looks fifteen seconds old, always.

    Issue #205 is about noticing that a host has been refusing for half an
    hour. That is unanswerable if the timestamp is rewritten on every beat.
    """
    _, updater = _updater(tmp_path, idle=False)
    updater.observe(_beat("0.1.4"))
    updater.maybe_activate()
    first = updater.status()["observed_at"]
    assert first is not None

    for _ in range(3):
        updater.observe(_beat("0.1.4"))
        updater.maybe_activate()

    status = updater.status()
    assert status["reason"] == "not_quiescent"
    assert status["observed_at"] == first


def test_observed_at_moves_when_the_reason_changes(tmp_path):
    """A different refusal is a new fact, and gets its own timestamp."""
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")
    busy = _state(idle=False)
    updater = HostUpdater(
        busy,
        layout,
        default_config(),
        installer=_broken_installer,
        restarter=lambda: None,
    )
    updater.observe(_beat("0.1.4"))
    updater.maybe_activate()
    assert updater.status()["reason"] == "smoke_test"
    first = updater.status()["observed_at"]

    # The binary is repaired in place; now only the busy host is in the way.
    _installed(layout, "0.1.4")
    updater.maybe_activate()

    status = updater.status()
    assert status["reason"] == "not_quiescent"
    assert status["observed_at"] != first


def test_a_failed_install_still_reports_which_version_failed(tmp_path):
    """`blocked: true, version: null` tells an operator nothing actionable."""
    _, updater = _updater(tmp_path, installs=False)
    updater.observe(_beat("0.1.4"))

    status = updater.status()
    assert status["update_blocked"] is True
    assert status["reason"] == "install_failed"
    # Cleared so the next beat retries the download...
    assert status["pending_version"] is None
    # ...but the hub still gets to say which version could not be installed.
    assert status["blocked_version"] == "0.1.4"


def test_repeated_install_failures_keep_the_first_timestamp(tmp_path):
    _, updater = _updater(tmp_path, installs=False)
    updater.observe(_beat("0.1.4"))
    first = updater.status()["observed_at"]
    assert first is not None

    for _ in range(3):
        updater.observe(_beat("0.1.4"))

    assert updater.status()["observed_at"] == first
    assert updater.status()["blocked_version"] == "0.1.4"


def test_an_install_that_finally_succeeds_clears_the_failure(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")
    attempts = []

    def flaky(lay, artifact, **kwargs):
        attempts.append(artifact.version)
        if len(attempts) == 1:
            return False
        _installed(lay, artifact.version)
        return True

    updater = HostUpdater(
        _state(idle=False),
        layout,
        default_config(),
        installer=flaky,
        restarter=lambda: None,
    )
    updater.observe(_beat("0.1.4"))
    assert updater.status()["reason"] == "install_failed"

    updater.observe(_beat("0.1.4"))

    assert attempts == ["0.1.4", "0.1.4"]
    status = updater.status()
    assert status["update_blocked"] is False
    assert status["reason"] is None
    assert status["blocked_version"] is None
    assert status["pending_version"] == "0.1.4"


def test_a_refusal_is_dropped_if_the_target_moved_while_we_checked(tmp_path):
    """Compare-and-set: the checks run with the lock released.

    Unreachable from the single heartbeat thread today, but a daemon-local
    "check now" endpoint would run on a ThreadingHTTPServer handler thread and
    could retarget mid-smoke-test. Losing that update would pin the host to a
    version the hub has already withdrawn.
    """
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")
    updater = HostUpdater(
        _state(),
        layout,
        default_config(),
        installer=_broken_installer,
        restarter=lambda: None,
    )

    def retarget_then_fail(version):
        # Stands in for another thread observing a retraction mid-check.
        updater.observe({})
        return False

    updater.observe(_beat("0.1.4"))
    layout.smoke_test = retarget_then_fail

    assert updater.maybe_activate() is False
    assert updater.status() == {
        "pending_version": None,
        "blocked_version": None,
        "update_blocked": False,
        "reason": None,
        "observed_at": None,
    }


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
