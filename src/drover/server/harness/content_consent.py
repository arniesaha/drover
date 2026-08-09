"""Host-local advisory content consent state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Mapping


class DurableContentConsent:
    """A monotonic, atomically persisted content-serving gate.

    Missing or malformed state is deliberately epoch zero and disabled.  The
    state contains no target paths or analyzed content, so it is safe to send
    over the host command plane and to retain across daemon restarts.
    """

    def __init__(self, path: str | Path, *, initial_enabled: bool = False) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        missing = not self.path.exists()
        self._state = self._load()
        if initial_enabled and missing:
            initial = {"enabled": True, "epoch": 1}
            self._persist(initial)
            self._state = initial

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def apply(self, *, enabled: bool, epoch: int) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if type(epoch) is not int or epoch < 0:
            raise ValueError("epoch must be a non-negative integer")
        if epoch == 0 and enabled:
            raise ValueError("epoch zero must be disabled")
        with self._lock:
            current_epoch = int(self._state["epoch"])
            if epoch < current_epoch:
                raise ValueError("stale consent epoch")
            if epoch == current_epoch:
                if enabled != self._state["enabled"]:
                    raise ValueError("consent epoch conflicts with current state")
                return dict(self._state)
            next_state = {"enabled": enabled, "epoch": epoch}
            self._persist(next_state)
            self._state = next_state
            return dict(self._state)

    def _load(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"enabled": False, "epoch": 0}
        if not _valid_state(loaded):
            return {"enabled": False, "epoch": 0}
        return {"enabled": loaded["enabled"], "epoch": loaded["epoch"]}

    def _persist(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(state), handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _valid_state(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"enabled", "epoch"}
        and type(value["enabled"]) is bool
        and type(value["epoch"]) is int
        and value["epoch"] > 0
    )
