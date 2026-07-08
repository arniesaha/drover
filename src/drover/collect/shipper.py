"""rsync-based shipper that moves staging JSONL into the server's incoming dir.

Runs ``rsync -a --remove-source-files <staging>/*.jsonl <host>:<remote_root>/<host_id>/``
and returns a structured result. Caller injects ``_runner`` for tests.

The shipper does NOT touch ``.tmp`` files — those represent in-progress
JSONL writes from the same run. ``write_events_jsonl`` only renames to
``.jsonl`` after fsync, so anything matching ``*.jsonl`` is safe to ship.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


class ShipError(RuntimeError):
    """rsync exited non-zero."""


@dataclass(frozen=True)
class ShipResult:
    files: int
    returncode: int
    command: Optional[list[str]]


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def ship_staging(
    *,
    staging_dir: Path,
    host: str,
    host_id: str,
    remote_root: str = "~/.drover/incoming",
    rsync: str = "rsync",
    extra_args: Optional[list[str]] = None,
    _runner: Optional[Callable] = None,
) -> ShipResult:
    """Ship every ``*.jsonl`` in ``staging_dir`` to the destination.

    When ``host`` is empty, the destination is local — we still use rsync
    (for atomic moves and ``--remove-source-files``) but skip the ssh
    transport entirely. This is the common case when the lakehouse and
    the shipper run on the same machine (e.g. the mac-mini).

    Returns ``ShipResult(files=0, command=None)`` without invoking rsync when
    no ``.jsonl`` is present.
    """
    runner = _runner or _default_runner
    files = sorted(staging_dir.glob("*.jsonl"))
    if not files:
        return ShipResult(files=0, returncode=0, command=None)

    if host:
        dest = f"{host}:{remote_root}/{host_id}/"
    else:
        # Local destination: expand `~` and ensure the per-host subdir exists.
        local_root = Path(remote_root).expanduser() / host_id
        local_root.mkdir(parents=True, exist_ok=True)
        dest = f"{local_root}/"

    cmd: list[str] = [rsync, "-a", "--remove-source-files"]
    if extra_args:
        cmd.extend(extra_args)
    cmd.extend(str(p) for p in files)
    cmd.append(dest)

    result = runner(cmd)
    if result.returncode != 0:
        raise ShipError(
            f"rsync exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return ShipResult(files=len(files), returncode=result.returncode, command=cmd)
