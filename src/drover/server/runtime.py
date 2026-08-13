"""Versioned runtime directories and the ``current`` symlink.

An update installs a whole new venv beside the old one and moves a symlink.
That is the entire reason rollback is cheap: nothing is ever upgraded in
place, so the previous version is still sitting there, intact, one atomic
rename away.

Service units point at ``runtime/current``, never at a version directory, so
activating a version is a symlink flip and a restart rather than a unit
rewrite.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

MARKER_FILENAME = "pending_verification.json"
SMOKE_TIMEOUT_SECONDS = 30


def _version_key(version: str) -> tuple[int, ...]:
    cleaned = version.lstrip("v")
    parts = []
    for chunk in cleaned.split("."):
        digits = "".join(character for character in chunk if character.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def compare_versions(left: str, right: str) -> int:
    """-1, 0, or 1.

    Numeric per component, so 0.10.0 sorts above 0.2.0. Lexical comparison
    gets that backwards, which would strand a fleet on 0.9.x forever.
    """
    a, b = _version_key(left), _version_key(right)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return (a > b) - (a < b)


class RuntimeLayout:
    """Reads and mutates ``<home>/runtime``. Never touches state beside it."""

    def __init__(self, home: Path) -> None:
        self._home = Path(home)

    @property
    def root(self) -> Path:
        return self._home / "runtime"

    @property
    def current(self) -> Path:
        return self.root / "current"

    def version_dir(self, version: str) -> Path:
        return self.root / version

    def executable(self, name: str, version: str | None = None) -> Path:
        base = self.current if version is None else self.version_dir(version)
        return base / "bin" / name

    def installed_versions(self) -> list[str]:
        if not self.root.is_dir():
            return []
        versions = [
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and not entry.is_symlink() and entry.name != "current"
        ]
        return sorted(versions, key=_version_key)

    def active_version(self) -> str | None:
        try:
            return os.readlink(self.current)
        except OSError:
            return None

    def flip(self, version: str) -> None:
        """Atomically repoint ``current``.

        Staged then renamed, because unlinking first would leave a window
        where the symlink does not exist at all, and every service unit
        resolves its executable through it.

        The link target is relative, so the whole ~/.drover tree can be moved
        or restored from a backup without leaving a dangling absolute path.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        staging = self.root / ".current.new"
        if staging.exists() or staging.is_symlink():
            staging.unlink()
        staging.symlink_to(version)
        os.replace(staging, self.current)

    def smoke_test(self, version: str) -> bool:
        """A version that cannot state its own version never gets the symlink."""
        binary = self.executable("drover-server", version)
        if not binary.exists():
            return False
        try:
            result = subprocess.run(
                [str(binary), "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=SMOKE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def prune(self, keep: int) -> list[str]:
        """Drop old versions, never the active one however old it is."""
        active = self.active_version()
        versions = self.installed_versions()
        keepers = set(versions[-keep:]) if keep > 0 else set()
        if active:
            keepers.add(active)
        removed = []
        for version in versions:
            if version in keepers:
                continue
            shutil.rmtree(self.version_dir(version), ignore_errors=True)
            removed.append(version)
        return removed

    def write_marker(self, previous: str, target: str) -> None:
        """Record what to fall back to if ``target`` cannot come up."""
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / MARKER_FILENAME).write_text(
            json.dumps({"previous": previous, "target": target}), encoding="utf-8"
        )

    def read_marker(self) -> tuple[str, str] | None:
        try:
            raw = json.loads((self.root / MARKER_FILENAME).read_text(encoding="utf-8"))
            return str(raw["previous"]), str(raw["target"])
        except (OSError, ValueError, KeyError, TypeError):
            # A half-written marker must read as absent rather than wedge a
            # host into rolling back on every start.
            return None

    def clear_marker(self) -> None:
        try:
            (self.root / MARKER_FILENAME).unlink()
        except OSError:
            return
