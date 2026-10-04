"""Durable, host-local retirement fence for legacy DERIVED writers only.

Every participating process uses the resolved store path's stable sidecar inode.
Raw/source ingestion does not enter this gate. Failure never re-enables writers.
"""

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .runtime import LakeError
from .serving import _SELECTION_LOCK, check_selected, selected_config

_LOCK = _SELECTION_LOCK
_HELD = {}
_MAX_STATE_BYTES = 4096


def _token(config):
    return hashlib.sha256(
        json.dumps(asdict(config), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _fence_path(path):
    path = Path(path).resolve()
    return path.with_name(path.name + ".legacy-derived-gate")


@contextmanager
def _durable_gate(path):
    """Exclusive across writers/activation; reentrant for nested decorated calls.

    Never rename/unlink this inode. Lock ownership lasts through the whole write,
    including its legacy reads. A dead process releases flock, not retirement.
    """
    path = _fence_path(path)
    with _LOCK:
        held = _HELD.get(path)
        if held is not None:
            yield held
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            handle = os.fdopen(descriptor, "r+b")
        except OSError:
            raise LakeError("lake_retirement_fence_unavailable") from None
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX)
            except OSError:
                raise LakeError("lake_retirement_fence_unavailable") from None
            _HELD[path] = handle
            try:
                yield handle
            finally:
                _HELD.pop(path)
        finally:
            handle.close()


def _after_fork():
    # A fork must not reuse the parent's locked open-file description. Closing
    # the child's copy (without LOCK_UN) leaves the parent's ownership intact.
    for handle in _HELD.values():
        handle.close()
    _HELD.clear()


os.register_at_fork(after_in_child=_after_fork)


def _read(handle):
    try:
        handle.seek(0)
        raw = handle.read(_MAX_STATE_BYTES + 1)
        if not raw:
            return None  # No activation has ever been requested for this store.
        state = json.loads(raw)
        if (
            len(raw) > _MAX_STATE_BYTES
            or not isinstance(state, dict)
            or set(state) != {"version", "status", "token"}
            or type(state["version"]) is not int
            or state["version"] != 1
            or state["status"] not in ("pending", "active")
            or not isinstance(state["token"], str)
            or len(state["token"]) != 64
            or any(c not in "0123456789abcdef" for c in state["token"])
        ):
            raise ValueError()
        return state
    except (OSError, ValueError, TypeError):
        raise LakeError("lake_retirement_state_invalid") from None


def _write(handle, config, status, path):
    encoded = json.dumps(
        {"version": 1, "status": status, "token": _token(config)}, sort_keys=True
    ).encode()
    try:
        # Write BEFORE truncation: a crash leaves the old latch or malformed
        # nonempty state (closed), never an empty file that authorizes legacy.
        handle.seek(0)
        handle.write(encoded)
        handle.flush()
        handle.truncate(len(encoded))
        os.fsync(handle.fileno())
        directory = os.open(_fence_path(path).parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise LakeError("lake_retirement_fence_unavailable") from None


def selection_changed(path, config):
    """Called under the selection lock BEFORE publishing a changed local config.

    A durable invalidation prevents even another process with the old config
    from reusing activation. No selection change clears the retirement latch.
    """
    with _durable_gate(path) as handle:
        state = _read(handle)
        if state is not None and state["token"] != _token(config):
            _write(handle, config, "pending", path)


def activate_retirement(path):
    """Explicit renewal, draining all participating processes before activation."""
    with _durable_gate(path) as handle:
        config = selected_config(path)
        if not config.retire_legacy_writers or config.backend != "ducklake":
            raise LakeError("lake_retirement_not_requested")
        _read(handle)  # Corrupt state needs operator repair, never silent reset.
        _write(handle, config, "pending", path)  # Persist closure BEFORE verification.
        check_selected(path)
        if selected_config(path) != config:
            raise LakeError("lake_retirement_renewal_required")
        _write(handle, config, "active", path)


@contextmanager
def legacy_derived_write(path):
    with _durable_gate(path) as handle:
        config = selected_config(path)
        state = _read(handle)
        if state is not None and (
            state["status"] != "active" or state["token"] != _token(config)
        ):
            raise LakeError("lake_retirement_renewal_required")
        if state is not None:
            check_selected(path)
            yield False
        elif config.retire_legacy_writers:
            raise LakeError("lake_retirement_not_activated")
        else:
            yield True


def fence_derived_writer(path_argument, retired_result):
    """Serialize the entire derived write, including its read of legacy state."""
    import functools
    import inspect

    def decorate(function):
        signature = inspect.signature(function)

        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            arguments = signature.bind(*args, **kwargs)
            arguments.apply_defaults()
            path = arguments.arguments[path_argument]
            if path is None:
                return function(*args, **kwargs)
            with legacy_derived_write(path) as allowed:
                if not allowed:
                    return retired_result()
                return function(*args, **kwargs)

        return wrapped

    return decorate
