"""Host-local advisory content consent state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
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
        self._state, self._valid, self._epoch_floor = self._load()
        if initial_enabled and missing:
            initial = {"enabled": True, "epoch": 1}
            self._persist(initial)
            self._state = initial
            self._valid = True
            self._epoch_floor = 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def reconciled(self, *, enabled: bool) -> bool:
        """Whether durable state is valid and matches central user intent."""

        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lock:
            return self._valid and self._state["enabled"] is enabled

    def reconcile(self, *, enabled: bool) -> dict[str, Any]:
        """Advance invalid or divergent state without reusing an observed epoch.

        A persistence failure immediately closes the in-memory content gate.
        The caller can then report the failed repair without permitting content
        access from the stale on-disk state.
        """

        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lock:
            if self._valid and self._state["enabled"] is enabled:
                return dict(self._state)
            next_epoch = max(int(self._state["epoch"]), self._epoch_floor) + 1
            next_state = {"enabled": enabled, "epoch": next_epoch}
            try:
                self._persist(next_state)
            except Exception:
                self._state = {"enabled": False, "epoch": next_epoch}
                self._valid = False
                self._epoch_floor = next_epoch
                raise
            self._state = next_state
            self._valid = True
            self._epoch_floor = next_epoch
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
            self._valid = True
            self._epoch_floor = epoch
            return dict(self._state)

    def _load(self) -> tuple[dict[str, Any], bool, int]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"enabled": False, "epoch": 0}, True, 0
        except OSError:
            return {"enabled": False, "epoch": 0}, False, 0
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            epoch = _observed_epoch(raw)
            return {"enabled": False, "epoch": epoch}, False, epoch
        if not _valid_state(loaded):
            epoch = _observed_epoch(loaded)
            return {"enabled": False, "epoch": epoch}, False, epoch
        return (
            {"enabled": loaded["enabled"], "epoch": loaded["epoch"]},
            True,
            int(loaded["epoch"]),
        )

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


def _observed_epoch(value: Any) -> int:
    """Recover a non-negative epoch floor from invalid durable content."""

    if isinstance(value, dict):
        epoch = value.get("epoch")
        return epoch if type(epoch) is int and epoch >= 0 else 0
    if isinstance(value, str):
        match = re.search(r'"epoch"\s*:\s*(\d+)', value)
        return int(match.group(1)) if match is not None else 0
    return 0
