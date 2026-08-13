"""Service units must set PATH explicitly and follow the current symlink.

Both properties have silently broken hosts before: a unit with no PATH cannot
find the agent CLIs it is meant to drive, and the symptom (sessions that never
start a driver) does not look like a PATH problem. A unit pinned to a version
directory would make an update a no-op rather than a symlink flip.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from drover.server.service_units import render_launchd, render_systemd, runtime_bin


def test_runtime_bin_points_at_the_symlink_not_a_version():
    assert (
        runtime_bin(Path("/home/x/.drover")).as_posix()
        == "/home/x/.drover/runtime/current/bin"
    )


def test_launchd_unit_is_valid_plist_with_explicit_path():
    rendered = render_launchd(
        "com.drover.server",
        "/home/x/.drover/runtime/current/bin/drover-server",
        ["run"],
        home=Path("/home/x"),
        path_entries=["/home/x/.drover/runtime/current/bin", "/usr/bin"],
    )
    parsed = plistlib.loads(rendered.encode("utf-8"))

    assert parsed["Label"] == "com.drover.server"
    assert parsed["ProgramArguments"][0].endswith("current/bin/drover-server")
    assert parsed["ProgramArguments"][1] == "run"
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert "PATH" in parsed["EnvironmentVariables"]
    assert "/usr/bin" in parsed["EnvironmentVariables"]["PATH"]


def test_launchd_logs_land_under_the_given_home():
    rendered = render_launchd(
        "com.drover.harnessd",
        "/bin/true",
        [],
        home=Path("/home/x"),
        path_entries=["/usr/bin"],
    )
    parsed = plistlib.loads(rendered.encode("utf-8"))
    assert parsed["StandardOutPath"] == "/home/x/Library/Logs/drover/harnessd.out.log"
    assert parsed["StandardErrorPath"] == "/home/x/Library/Logs/drover/harnessd.err.log"


def test_launchd_unit_escapes_xml_in_arguments():
    rendered = render_launchd(
        "com.drover.server",
        "/bin/true",
        ["--label", "A & B <c>"],
        home=Path("/home/x"),
        path_entries=["/usr/bin"],
    )
    parsed = plistlib.loads(rendered.encode("utf-8"))
    assert parsed["ProgramArguments"][2] == "A & B <c>"


def test_systemd_unit_sets_path_and_restarts():
    rendered = render_systemd(
        "Drover harness daemon",
        "/home/x/.drover/runtime/current/bin/drover-harnessd",
        ["--host-id", "build-mac"],
        path_entries=["/home/x/.drover/runtime/current/bin", "/usr/bin"],
    )
    assert "Environment=PATH=" in rendered
    assert "Restart=always" in rendered
    assert "WantedBy=default.target" in rendered
    assert (
        "ExecStart=/home/x/.drover/runtime/current/bin/drover-harnessd "
        "--host-id build-mac" in rendered
    )


def test_systemd_quotes_arguments_containing_spaces():
    rendered = render_systemd(
        "Drover",
        "/bin/true",
        ["--display-name", "Build Mac"],
        path_entries=["/usr/bin"],
    )
    assert '"Build Mac"' in rendered
    assert "--display-name" in rendered


def test_systemd_with_no_arguments_has_no_trailing_space():
    rendered = render_systemd("Drover", "/bin/true", [], path_entries=["/usr/bin"])
    exec_line = next(
        line for line in rendered.splitlines() if line.startswith("ExecStart=")
    )
    assert exec_line == "ExecStart=/bin/true"


def test_both_renderers_follow_current_rather_than_a_version():
    """The whole update story is a symlink flip; a pinned path breaks it."""
    binary = runtime_bin(Path("/home/x/.drover")) / "drover-server"
    launchd = render_launchd(
        "com.drover.server",
        str(binary),
        [],
        home=Path("/home/x"),
        path_entries=[str(runtime_bin(Path("/home/x/.drover")))],
    )
    systemd = render_systemd(
        "Drover",
        str(binary),
        [],
        path_entries=[str(runtime_bin(Path("/home/x/.drover")))],
    )
    for rendered in (launchd, systemd):
        assert "runtime/current/bin" in rendered
        # A version-pinned path is the bug this guards against.
        assert "runtime/0." not in rendered


def test_keep_alive_defaults_true_for_daemons():
    rendered = render_launchd(
        "com.drover.server",
        "/bin/true",
        [],
        home=Path("/home/x"),
        path_entries=["/usr/bin"],
    )
    assert plistlib.loads(rendered.encode("utf-8"))["KeepAlive"] is True


def test_keep_alive_can_be_turned_off_for_one_shots():
    """launchd restarts a KeepAlive job the moment it exits, including on
    success, which turns a run-once script into a restart loop. This fleet
    has already lost a release-verification script to exactly that."""
    rendered = render_launchd(
        "com.drover.release.verify",
        "/bin/true",
        [],
        home=Path("/home/x"),
        path_entries=["/usr/bin"],
        keep_alive=False,
    )
    assert plistlib.loads(rendered.encode("utf-8"))["KeepAlive"] is False
