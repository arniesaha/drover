"""Versioned runtime directories and the ``current`` symlink.

An update installs a whole new venv beside the old one and moves a symlink.
That is the entire reason rollback is cheap: nothing is ever upgraded in
place, so the previous version is still sitting there, intact, one atomic
rename away.

Service units point at ``runtime/current``, never at a version directory, so
activating a version is a symlink flip and a restart rather than a unit
rewrite.

One host cannot work that way. A macOS hub whose venv lives on an external
volume holds a TCC grant keyed to the executable, and a new venv is a new
executable, so flipping to it loses the grant and the service dies reading its
own ``pyvenv.cfg``. Such a host opts into installing into the venv it already
has (see ``update_activation`` in the config), which is why each version tree
also caches the artifacts it was built from: that is the material an in-place
activation, and its rollback, install from.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("drover.runtime")

MARKER_FILENAME = "pending_verification.json"
SMOKE_TIMEOUT_SECONDS = 30


def _smoke_excerpt(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return " | ".join((output or "").strip().splitlines()[:4])[:2048] or "no output"


# Where a version keeps the artifacts it was built from. Inside the version
# tree on purpose: `prune` already drops whole version directories, so the
# cache inherits that lifetime rather than needing one of its own to get wrong.
ARTIFACT_DIRNAME = ".artifact"
ARTIFACT_MANIFEST = "artifact.json"


@dataclass(frozen=True)
class CachedArtifact:
    """The verified wheel and lock a version was installed from."""

    wheel: Path
    lock: Path


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
    """Reads and mutates a runtime root, defaulting to ``<home>/runtime``."""

    def __init__(self, home: Path, *, root: Path | None = None) -> None:
        self._home = Path(home)
        self._root = Path(root) if root is not None else self._home / "runtime"

    @property
    def root(self) -> Path:
        return self._root

    @property
    def current(self) -> Path:
        return self.root / "current"

    def version_dir(self, version: str) -> Path:
        return self.root / version

    def artifact_dir(self, version: str) -> Path:
        return self.version_dir(version) / ARTIFACT_DIRNAME

    def cache_artifact(self, version: str, wheel: Path, lock: Path) -> bool:
        """Keep the verified wheel and lock beside the version they built.

        Best effort: a host that activates by flipping a symlink never reads
        these, so failing to cache must not fail an otherwise good install.
        """
        directory = self.artifact_dir(version)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(wheel, directory / Path(wheel).name)
            shutil.copy2(lock, directory / Path(lock).name)
            (directory / ARTIFACT_MANIFEST).write_text(
                json.dumps({"wheel": Path(wheel).name, "lock": Path(lock).name}),
                encoding="utf-8",
            )
        except OSError:
            log.warning("could not cache the artifacts for %s", version, exc_info=True)
            return False
        return True

    def cached_artifact(self, version: str) -> CachedArtifact | None:
        """The artifacts ``version`` was installed from, or None.

        None covers a version installed before this cache existed as well as a
        half-written one, and both mean the same thing to a caller: there is
        nothing here it may install.
        """
        directory = self.artifact_dir(version)
        try:
            raw = json.loads(
                (directory / ARTIFACT_MANIFEST).read_text(encoding="utf-8")
            )
            # `.name` because these are filenames, not paths: nothing in the
            # manifest gets to point outside the directory it lives in.
            wheel = directory / Path(str(raw["wheel"])).name
            lock = directory / Path(str(raw["lock"])).name
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not wheel.is_file() or not lock.is_file():
            return None
        return CachedArtifact(wheel=wheel, lock=lock)

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
        try:
            binary.stat()
        except FileNotFoundError:
            log.warning("smoke test executable is missing: %s", binary)
            return False
        except OSError as exc:
            log.warning("smoke test cannot access %s: %s", binary, exc)
            return False
        try:
            result = subprocess.run(
                [str(binary), "--version"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=SMOKE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            log.warning(
                "smoke test timed out for %s: %s",
                binary,
                _smoke_excerpt(exc.stderr or exc.stdout),
            )
            return False
        except OSError as exc:
            log.warning("smoke test could not run %s: %s", binary, exc)
            return False
        if result.returncode != 0:
            log.warning(
                "smoke test failed for %s (exit %s): %s",
                binary,
                result.returncode,
                _smoke_excerpt(result.stderr or result.stdout),
            )
            return False
        return True

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
