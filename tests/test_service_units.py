"""Service units must set PATH explicitly and follow the current symlink.

Both properties have silently broken hosts before: a unit with no PATH cannot
find the agent CLIs it is meant to drive, and the symptom (sessions that never
start a driver) does not look like a PATH problem. A unit pinned to a version
directory would make an update a no-op rather than a symlink flip.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
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
    assert parsed["EnvironmentVariables"]["HOME"] == "/home/x"
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


def test_launchd_gives_a_clean_shutdown_exit_timeout():
    """SIGTERM must get a checkpoint window before launchd SIGKILLs (#307).

    A server killed mid-checkpoint starts the next open from a SIGKILLed file
    and pays for recovery, which on the reference hub was a 4.5-minute first
    DuckDB open. A generous ExitTimeOut lets the SIGTERM handler finish its
    checkpoint and exit cleanly.
    """
    rendered = render_launchd(
        "com.drover.server",
        "/home/x/.drover/runtime/current/bin/drover-server",
        ["run"],
        home=Path("/home/x"),
        path_entries=["/usr/bin"],
    )
    parsed = plistlib.loads(rendered.encode("utf-8"))
    assert parsed["ExitTimeOut"] == 300


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


def test_launchd_reads_service_environment_from_a_private_file():
    rendered = render_launchd(
        "com.drover.server",
        "/home/x/.drover/runtime/current/bin/drover-server",
        ["run"],
        home=Path("/home/x"),
        path_entries=["/usr/bin"],
        environment_file=Path("/home/x/.drover/server.env"),
    )

    parsed = plistlib.loads(rendered.encode("utf-8"))
    assert parsed["EnvironmentVariables"] == {"HOME": "/home/x", "PATH": "/usr/bin"}
    assert parsed["ProgramArguments"] == [
        "/bin/sh",
        "-c",
        'set -a; . "$1" || exit $?; shift; exec "$@"',
        "drover-service-env",
        "/home/x/.drover/server.env",
        "/home/x/.drover/runtime/current/bin/drover-server",
        "run",
    ]


def test_launchd_environment_file_reaches_the_daemon(tmp_path: Path):
    env_file = tmp_path / "server.env"
    env_file.write_text("DROVER_CONTROL_DSN='postgresql://private-target'\n")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os\n"
        "import sys\n"
        f"assert sys.argv == [{str(probe)!r}, 'run']\n"
        "print(os.environ['DROVER_CONTROL_DSN'])\n"
    )
    rendered = render_launchd(
        "com.drover.server",
        sys.executable,
        [str(probe), "run"],
        home=tmp_path,
        path_entries=["/usr/bin"],
        environment_file=env_file,
    )
    argv = plistlib.loads(rendered.encode("utf-8"))["ProgramArguments"]

    result = subprocess.run(argv, check=True, capture_output=True, text=True)

    assert result.stdout == "postgresql://private-target\n"


def test_launchd_environment_sets_home_without_secret_values():
    rendered = render_launchd(
        "com.drover.harnessd",
        "/home/x/.drover/runtime/current/bin/drover-harnessd",
        ["--host-id", "build-mac"],
        home=Path("/home/x"),
        path_entries=[
            "/home/x/.drover/runtime/current/bin",
            "/home/x/.local/bin",
            "/usr/bin",
        ],
    )
    parsed = plistlib.loads(rendered.encode("utf-8"))

    assert parsed["EnvironmentVariables"]["HOME"] == "/home/x"
    assert "/home/x/.local/bin" in parsed["EnvironmentVariables"]["PATH"]
    assert "DROVER_API_TOKEN" not in rendered
    assert "api_token" not in rendered


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


def test_systemd_reads_service_environment_from_a_private_file():
    rendered = render_systemd(
        "Drover server",
        "/home/x/.drover/runtime/current/bin/drover-server",
        ["run"],
        path_entries=["/home/x/.drover/runtime/current/bin", "/usr/bin"],
        environment_file=Path("/home/x/.drover/server.env"),
    )

    assert "EnvironmentFile=/home/x/.drover/server.env" in rendered
    assert "DROVER_CONTROL_DSN" not in rendered


def test_systemd_quotes_an_environment_file_path_with_spaces():
    rendered = render_systemd(
        "Drover server",
        "/bin/true",
        [],
        path_entries=["/usr/bin"],
        environment_file=Path("/home/Build User/.drover/server.env"),
    )

    assert 'EnvironmentFile="/home/Build User/.drover/server.env"' in rendered


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


def _complete_job():
    return render_launchd(
        "com.drover.server",
        "/example/bin/drover-server",
        ["run"],
        home=Path("/example"),
        path_entries=["/usr/bin"],
    )


def test_array_extraction_cannot_replace_a_job(tmp_path):
    import pytest

    from drover.server.service_units import write_launchd

    path = tmp_path / "com.drover.server.plist"
    path.write_text(_complete_job())
    before = path.read_bytes()
    argv = plistlib.loads(before)["ProgramArguments"]
    with pytest.raises(ValueError, match="dictionary"):
        write_launchd(path, plistlib.dumps(argv))
    assert path.read_bytes() == before


def test_incomplete_dict_cannot_replace_a_job(tmp_path):
    import pytest

    from drover.server.service_units import write_launchd

    path = tmp_path / "com.drover.server.plist"
    path.write_text(_complete_job())
    before = path.read_bytes()
    job = plistlib.loads(before)
    for key in ("Label", "EnvironmentVariables", "KeepAlive", "RunAtLoad"):
        broken = dict(job)
        del broken[key]
        with pytest.raises(ValueError):
            write_launchd(path, plistlib.dumps(broken))
        assert path.read_bytes() == before


def test_launchd_replace_is_atomic_and_preserves_permissions(tmp_path, monkeypatch):
    import os

    from drover.server.service_units import write_launchd

    path = tmp_path / "com.drover.server.plist"
    path.write_text(_complete_job())
    path.chmod(0o600)
    before = path.read_bytes()
    job = plistlib.loads(before)
    job["ProgramArguments"] = ["/new/bin/drover-server", "run"]
    original = os.replace

    def replace(source, destination):
        assert path.read_bytes() == before
        assert plistlib.loads(Path(source).read_bytes())["Label"] == "com.drover.server"
        original(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    write_launchd(path, plistlib.dumps(job))
    assert (
        plistlib.loads(path.read_bytes())["ProgramArguments"][0]
        == "/new/bin/drover-server"
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_failed_atomic_replace_keeps_original_and_cleans_staging(tmp_path, monkeypatch):
    import os

    import pytest

    from drover.server.service_units import write_launchd

    path = tmp_path / "com.drover.server.plist"
    path.write_text(_complete_job())
    before = path.read_bytes()

    def fail(*args):
        raise OSError("replacement failed")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        write_launchd(path, _complete_job())
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_plutil_xml_extraction_overwrites_input_without_stdout_option(tmp_path):
    import shutil

    import pytest

    if sys.platform != "darwin" or not shutil.which("plutil"):
        pytest.skip("macOS plutil characterization")
    path = tmp_path / "com.drover.server.plist"
    path.write_text(_complete_job())
    before = path.read_bytes()
    safe = subprocess.run(
        ["plutil", "-extract", "ProgramArguments", "xml1", "-o", "-", str(path)],
        capture_output=True,
        check=True,
    )
    assert path.read_bytes() == before
    assert isinstance(plistlib.loads(safe.stdout), list)
    subprocess.run(
        ["plutil", "-extract", "ProgramArguments", "xml1", str(path)], check=True
    )
    assert isinstance(plistlib.loads(path.read_bytes()), list)
