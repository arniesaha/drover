"""Render launchd and systemd units for the Drover daemons.

Generated rather than sed-substituted from a template so the two properties
that have silently broken hosts before are actually testable:

- an explicit ``PATH``, because a unit that inherits nothing cannot find the
  agent CLIs it exists to drive, and the symptom (structured sessions that
  never start a driver) does not look like a PATH problem;
- pointing at ``runtime/current`` rather than a version directory, because
  the whole update story is a symlink flip and a pinned path silently makes
  updates a no-op.
"""

from __future__ import annotations

import os
import plistlib
import tempfile
from pathlib import Path
from xml.parsers.expat import ExpatError

# Seconds launchd waits after SIGTERM before SIGKILL. The server checkpoints
# on SIGTERM; killing it mid-checkpoint makes the next open pay for recovery
# (#307: a 4.5-minute first DuckDB open). Generous enough for a clean
# checkpoint, short enough that a genuinely wedged unit still gets reclaimed.
EXIT_TIMEOUT_SECONDS = 300


def validate_launchd(content: str | bytes) -> dict:
    """Load a complete daemon job, never an extracted plist fragment.

    Do not include values in errors: environments may contain credentials.
    ``plutil -lint`` alone accepts arrays and incomplete job dictionaries.
    """
    try:
        data = plistlib.loads(content.encode() if isinstance(content, str) else content)
    except (ValueError, TypeError, ExpatError):
        raise ValueError("launchd job plist is malformed") from None
    if not isinstance(data, dict):
        raise ValueError("launchd job must be a dictionary")
    for key in ("Label", "StandardOutPath", "StandardErrorPath"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"launchd job requires {key}")
    args = data.get("ProgramArguments")
    if (
        not isinstance(args, list)
        or not args
        or not all(isinstance(arg, str) and arg for arg in args)
    ):
        raise ValueError("launchd job requires nonempty ProgramArguments")
    env = data.get("EnvironmentVariables")
    if (
        not isinstance(env, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
        or not env.get("HOME")
        or not env.get("PATH")
    ):
        raise ValueError("launchd job requires EnvironmentVariables with HOME and PATH")
    for key in ("KeepAlive", "RunAtLoad"):
        if type(data.get(key)) is not bool:
            raise ValueError(f"launchd job requires boolean {key}")
    if type(data.get("ExitTimeOut")) is not int or data["ExitTimeOut"] <= 0:
        raise ValueError("launchd job requires positive ExitTimeOut")
    return data


def write_launchd(path: Path, content: str | bytes) -> None:
    """Validate, round-trip and atomically replace a complete job.

    Existing file permissions survive; staging is private until validation.
    All writers and operator edits should use this boundary.
    """
    path = Path(path)
    job = validate_launchd(content)
    if path.stem != job["Label"]:
        raise ValueError("launchd job Label must match destination filename")
    serialized = plistlib.dumps(job)
    if validate_launchd(serialized) != job:
        raise ValueError("launchd job round-trip mismatch")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    staging = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".launchd-", delete=False
        ) as stream:
            staging = Path(stream.name)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        if validate_launchd(staging.read_bytes()) != job:
            raise ValueError("launchd staged job round-trip mismatch")
        staging.chmod(mode)
        os.replace(staging, path)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def runtime_bin(home: Path) -> Path:
    """Executables always resolve through the symlink, so an update is a flip."""
    return Path(home) / "runtime" / "current" / "bin"


def _systemd_unit_path(path: Path) -> str:
    """Render a unit-file path without applying shell-only quoting rules."""
    raw = str(path)
    if all(character.isalnum() or character in "/._-" for character in raw):
        return raw
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_launchd(
    label: str,
    program: str,
    arguments: list[str],
    *,
    home: Path,
    path_entries: list[str],
    keep_alive: bool = True,
    environment_file: Path | None = None,
) -> str:
    """A launchd job as plist XML.

    Built through ``plistlib`` rather than string formatting so an argument
    containing ``&`` or ``<`` cannot produce a plist launchd refuses to parse.

    ``keep_alive`` defaults true because the only jobs rendered here are
    long-running daemons that must come back after a crash. Pass false for
    anything one-shot: launchd restarts a ``KeepAlive`` job the moment it
    exits, *including when it exits successfully*, which turns a script that
    runs once into a restart loop. That has already happened on this fleet
    once, to a release-verification script that restarted 112 times.
    """
    log_dir = Path(home) / "Library" / "Logs" / "drover"
    short = label.rsplit(".", 1)[-1]
    program_arguments = [program, *arguments]
    environment_variables = {
        "HOME": str(Path(home)),
        "PATH": ":".join(path_entries),
    }
    if environment_file is not None:
        # launchd has no EnvironmentFile equivalent. Keep the secret in the
        # private file and use a fixed shell wrapper to export it before the
        # daemon starts, rather than writing the value into the plist.
        program_arguments = [
            "/bin/sh",
            "-c",
            'set -a; . "$1" || exit $?; shift; exec "$@"',
            "drover-service-env",
            str(environment_file),
            *program_arguments,
        ]
    payload = {
        "Label": label,
        "ProgramArguments": program_arguments,
        "EnvironmentVariables": environment_variables,
        "KeepAlive": keep_alive,
        "ExitTimeOut": EXIT_TIMEOUT_SECONDS,
        "RunAtLoad": True,
        "StandardOutPath": str(log_dir / f"{short}.out.log"),
        "StandardErrorPath": str(log_dir / f"{short}.err.log"),
    }
    rendered = plistlib.dumps(payload)
    validate_launchd(rendered)
    return rendered.decode("utf-8")


def render_systemd(
    description: str,
    program: str,
    arguments: list[str],
    *,
    path_entries: list[str],
    environment_file: Path | None = None,
) -> str:
    """A systemd user unit.

    systemd splits ``ExecStart`` on whitespace and honours double quotes, so
    only arguments containing a space are quoted; quoting everything would be
    noise in a file people read while debugging.
    """
    rendered_args = " ".join(
        f'"{argument}"' if " " in argument else argument for argument in arguments
    )
    exec_start = f"{program} {rendered_args}".rstrip()
    environment_file_line = (
        f"EnvironmentFile={_systemd_unit_path(environment_file)}\n"
        if environment_file is not None
        else ""
    )
    return f"""[Unit]
Description={description}
After=network-online.target

[Service]
Type=simple
Environment=PATH={":".join(path_entries)}
{environment_file_line}ExecStart={exec_start}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
