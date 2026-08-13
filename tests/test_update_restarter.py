"""The updater has to restart the unit it is actually running under.

`default_restarter` hardcoded `drover-harnessd.service`. The storage host's
unit is `drover-nas-harnessd.service`, so on that machine activation installed
the new version, flipped the symlink, wrote the rollback marker, and then ran
`systemctl restart` against a unit that does not exist. `check=False` swallowed
the failure, so the host kept running the old code with a stale marker on disk
and no log line saying why.

That matters more than a missed restart. The rollback watchdog only runs at
startup, so a host that cannot restart itself cannot self-heal -- which is the
entire scenario the watchdog exists for, on the machine that is hardest to
reach by hand.
"""

from __future__ import annotations

from drover.server.harness import updater as updater_module

_NAS_CGROUP = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "drover-nas-harnessd.service\n"
)


def test_the_systemd_unit_is_read_from_our_own_cgroup(tmp_path):
    """This is the bug: the NAS unit is not the conventional name."""
    cgroup = tmp_path / "cgroup"
    cgroup.write_text(_NAS_CGROUP, encoding="utf-8")

    assert updater_module._systemd_unit(cgroup) == "drover-nas-harnessd.service"


def test_a_conventional_unit_is_still_found(tmp_path):
    cgroup = tmp_path / "cgroup"
    cgroup.write_text(
        "0::/user.slice/user-1000.slice/user@1000.service/drover-harnessd.service\n",
        encoding="utf-8",
    )

    assert updater_module._systemd_unit(cgroup) == "drover-harnessd.service"


def test_an_unreadable_cgroup_falls_back_to_the_conventional_name(tmp_path):
    """Containers and odd init systems must not lose the restart entirely."""
    assert updater_module._systemd_unit(tmp_path / "missing") == (
        "drover-harnessd.service"
    )


def test_a_cgroup_naming_no_service_falls_back(tmp_path):
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/user.slice/session-3.scope\n", encoding="utf-8")

    assert updater_module._systemd_unit(cgroup) == "drover-harnessd.service"


def test_the_launchd_label_comes_from_the_job_that_started_us(monkeypatch):
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.drover.harnessd.custom")

    assert updater_module._launchd_label() == "com.drover.harnessd.custom"


def test_a_process_launchd_did_not_start_falls_back(monkeypatch):
    """launchd reports `0` for anything it did not start as a job."""
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")

    assert updater_module._launchd_label() == "com.drover.harnessd"


def test_restarting_on_linux_targets_the_discovered_unit(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(updater_module.sys, "platform", "linux")
    monkeypatch.setattr(
        updater_module, "_systemd_unit", lambda *a: "drover-nas-harnessd.service"
    )
    monkeypatch.setattr(
        updater_module.subprocess, "run", lambda cmd, **kw: calls.append(cmd)
    )

    updater_module.default_restarter()

    assert calls == [["systemctl", "--user", "restart", "drover-nas-harnessd.service"]]


def test_restarting_on_macos_targets_the_discovered_label(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(updater_module.sys, "platform", "darwin")
    monkeypatch.setattr(updater_module, "_launchd_label", lambda: "com.drover.x")
    monkeypatch.setattr(updater_module.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        updater_module.subprocess, "run", lambda cmd, **kw: calls.append(cmd)
    )

    updater_module.default_restarter()

    assert calls == [["launchctl", "kickstart", "-k", "gui/501/com.drover.x"]]
