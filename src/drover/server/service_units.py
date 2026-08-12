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

import plistlib
from pathlib import Path


def runtime_bin(home: Path) -> Path:
    """Executables always resolve through the symlink, so an update is a flip."""
    return Path(home) / "runtime" / "current" / "bin"


def render_launchd(
    label: str,
    program: str,
    arguments: list[str],
    *,
    home: Path,
    path_entries: list[str],
) -> str:
    """A launchd job as plist XML.

    Built through ``plistlib`` rather than string formatting so an argument
    containing ``&`` or ``<`` cannot produce a plist launchd refuses to parse.
    """
    log_dir = Path(home) / "Library" / "Logs" / "drover"
    short = label.rsplit(".", 1)[-1]
    payload = {
        "Label": label,
        "ProgramArguments": [program, *arguments],
        "EnvironmentVariables": {"PATH": ":".join(path_entries)},
        "KeepAlive": True,
        "RunAtLoad": True,
        "StandardOutPath": str(log_dir / f"{short}.out.log"),
        "StandardErrorPath": str(log_dir / f"{short}.err.log"),
    }
    return plistlib.dumps(payload).decode("utf-8")


def render_systemd(
    description: str,
    program: str,
    arguments: list[str],
    *,
    path_entries: list[str],
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
    return f"""[Unit]
Description={description}
After=network-online.target

[Service]
Type=simple
Environment=PATH={":".join(path_entries)}
ExecStart={exec_start}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
