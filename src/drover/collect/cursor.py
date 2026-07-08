"""Per-source cursor checkpoint store with advisory file locking.

Cursors live at ``<state_dir>/<source>.cursor`` (JSON) with sibling
``<source>.lock`` for advisory ``flock``. Writes go through a ``.tmp``
file + ``os.replace`` so a crash never leaves a partial cursor visible.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class CursorLocked(RuntimeError):
    """Raised when a non-blocking lock acquisition fails (another collector holds it)."""


@dataclass(frozen=True)
class CursorStore:
    state_dir: Path

    def _cursor_path(self, source_id: str) -> Path:
        return self.state_dir / f"{source_id}.cursor"

    def _lock_path(self, source_id: str) -> Path:
        return self.state_dir / f"{source_id}.lock"

    def read(self, source_id: str) -> dict:
        path = self._cursor_path(source_id)
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def write(self, source_id: str, payload: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        final = self._cursor_path(source_id)
        tmp = final.with_suffix(final.suffix + ".tmp")
        data = json.dumps(payload, sort_keys=True)
        with open(tmp, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)

    @contextmanager
    def lock(self, source_id: str) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path(source_id)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    raise CursorLocked(
                        f"another collector holds the lock for source {source_id!r}"
                    ) from e
                raise
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
