"""Explicit staging credential sources; never consult an OS credential store."""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from pathlib import Path
from typing import Sequence

STAGING_CREDENTIAL_BOUNDARY_VERSION = 1


def is_staging() -> bool:
    return os.environ.get("DROVER_RELEASE_ROLE") == "testflight-staging"


def staging_root() -> Path:
    root = Path(os.environ.get("DROVER_STAGING_ROOT", ""))
    if (
        not is_staging()
        or not root.is_absolute()
        or root.resolve() != root
        or Path.home() != root / "home"
    ):
        raise ValueError("invalid staging credential root")
    return root


def staging_session_paths(cwd: str | None) -> tuple[str, Path]:
    """Resolve a launch and its Git worktrees inside the dedicated workspace."""
    try:
        workspace = staging_root() / "workspace"
        if workspace.resolve(strict=True) != workspace or not workspace.is_dir():
            raise ValueError
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError
        requested = Path(cwd).expanduser() if cwd else workspace
        resolved = (workspace / requested).resolve(strict=True)
        worktrees = (workspace / ".worktrees").resolve()
        if (
            not resolved.is_relative_to(workspace)
            or not resolved.is_dir()
            or not worktrees.is_relative_to(workspace)
        ):
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise ValueError("staging cwd must stay within its workspace") from None
    return str(resolved), worktrees


def read_api_key() -> str:
    root = staging_root()
    path = root / "home/.drover/anthropic_api_key"
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("invalid staging credential path")
        if item == root:
            break
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise ValueError("staging API key must be a private regular file")
        value = stream.read(16385).strip()
    if not value or len(value) > 16384 or any(char.isspace() for char in value):
        raise ValueError("invalid staging API key")
    return value


def claude_command(command: Sequence[str]) -> list[str]:
    result = list(command)
    if not is_staging():
        return result
    read_api_key()  # Fail closed before spawning if no private source exists.
    if any(arg in ("--settings", "--setting-sources", "--bare") for arg in result):
        raise ValueError("staging Claude settings are fixed by the runtime")
    helper = shlex.join([sys.executable, "-m", "drover.server.staging_credentials"])
    return [
        *result,
        "--bare",
        "--setting-sources",
        "",
        "--settings",
        json.dumps({"apiKeyHelper": helper}),
    ]


def codex_command(command: Sequence[str]) -> list[str]:
    if not is_staging():
        return list(command)
    root = staging_root()
    if os.environ.get("CODEX_HOME") != str(root / "home/.codex"):
        raise ValueError("Codex must use the staging credential home")
    return [*command, "-c", 'cli_auth_credentials_store="file"']


def main() -> int:
    try:
        value = read_api_key()
    except (OSError, ValueError):
        print("staging credential unavailable", file=sys.stderr)
        return 1
    # The only consumer is Claude's apiKeyHelper pipe. Never log or export it.
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
