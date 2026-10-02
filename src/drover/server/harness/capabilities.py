"""Bounded public capability envelope; never serialize runtime adapter state.

Missing matrices remain missing for legacy clients. Matrix consumers must treat
absence as no advertised operations, not infer features from a harness name.
"""

from __future__ import annotations

import json
import re
from typing import Any

from drover.server.harness.adapters import HarnessCapabilities

MAX_ENVELOPE_BYTES = 64 * 1024
MAX_MATRIX_BYTES = 4096
MAX_HARNESSES = 32
MAX_ATTACHMENT_TYPES = 16
_BOOL_FIELDS = (
    "approvals",
    "interrupt",
    "native_resume",
    "model_catalog",
    "usage",
    "worktree",
    "interactive_auth",
)
# Additive v1 flag (#420): model/effort overrides reach later turns of a
# running session. Projected from the adapter's immutable
# `turn_preferences_mutable` class attribute, which turn dispatch already
# enforces. Older hosts omit it, so clients read it as false (fail closed).
_TURN_PREFERENCES = "turn_preferences"
_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_MIME = re.compile(r"[a-z0-9.+-]+/(?:[a-z0-9.+-]+|\*)\Z")


class InvalidCapabilities(ValueError):
    """A public declaration is unsafe. Messages never include supplied values."""


def unique_capability_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object hook for registration bodies and previously stored JSON."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidCapabilities("duplicate capability payload key")
        result[key] = value
    return result


def _bounded_json(value: Any, limit: int) -> None:
    # Bound unknown fields too, before discarding them. Iteration stops without
    # building another unbounded encoded copy of a registration or stored row.
    size = 0
    try:
        for chunk in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(
            value
        ):
            size += len(chunk)
            if size > limit:
                raise InvalidCapabilities("capability payload exceeds size limit")
    except (TypeError, ValueError, RecursionError):
        raise InvalidCapabilities("invalid or oversized capability payload") from None


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise InvalidCapabilities("invalid capability text field")
    return value


def _identity(value: Any) -> str:
    value = _text(value, 64)
    if not _ID.fullmatch(value):
        raise InvalidCapabilities("invalid harness identity")
    return value


def _legacy_identity(value: Any) -> str:
    # Pre-registry hosts could use display-like names (e.g. "codex beta").
    # Keep that metadata compatible without treating it as an adapter ID.
    value = _text(value, 256)
    if not value.strip():
        raise InvalidCapabilities("invalid legacy harness identity")
    return value


def capability_matrix(
    harness_id: str,
    capabilities: HarnessCapabilities,
    *,
    turn_preferences: bool = False,
) -> dict[str, Any]:
    """Only immutable contract fields cross the wire; no hooks are invoked."""
    return {
        "schema_version": 1,
        "harness_id": harness_id,
        "launch_modes": sorted(capabilities.launch_modes),
        **{name: getattr(capabilities, name) for name in _BOOL_FIELDS},
        # Preferences without a catalog have nothing to choose from.
        _TURN_PREFERENCES: bool(turn_preferences and capabilities.model_catalog),
        "attachments": sorted(capabilities.attachments),
    }


def _matrix(value: Any, harness_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidCapabilities("capabilities matrix must be an object")
    _bounded_json(value, MAX_MATRIX_BYTES)
    version = value.get("schema_version")
    if type(version) is not int or version < 1:
        raise InvalidCapabilities("invalid capability schema version")
    if value.get("harness_id", harness_id) != harness_id:
        raise InvalidCapabilities("capability harness identity mismatch")
    if version != 1:
        # Do not interpret future semantics as v1, or mistake them for legacy.
        result = {
            "schema_version": version,
            "harness_id": harness_id,
            "launch_modes": [],
        }
        _bounded_json(result, MAX_MATRIX_BYTES)
        return result
    modes = value.get("launch_modes")
    if (
        not isinstance(modes, list)
        or len(modes) > 2
        or any(
            not isinstance(mode, str) or mode not in {"structured", "pty"}
            for mode in modes
        )
        or len(set(modes)) != len(modes)
    ):
        raise InvalidCapabilities("invalid or duplicate launch modes")
    result = {
        "schema_version": 1,
        "harness_id": harness_id,
        "launch_modes": sorted(modes),
    }
    for name in (*_BOOL_FIELDS, _TURN_PREFERENCES):
        flag = value.get(name, False)
        if type(flag) is not bool:
            raise InvalidCapabilities("capability flags must be booleans")
        result[name] = flag
    attachments = value.get("attachments", [])
    if (
        not isinstance(attachments, list)
        or len(attachments) > MAX_ATTACHMENT_TYPES
        or any(
            not isinstance(mime, str) or len(mime) > 127 or not _MIME.fullmatch(mime)
            for mime in attachments
        )
        or len(set(attachments)) != len(attachments)
    ):
        raise InvalidCapabilities("invalid or duplicate attachment types")
    result["attachments"] = sorted(attachments)
    _bounded_json(result, MAX_MATRIX_BYTES)
    return result


def validate_capabilities(value: Any, host_id: str) -> dict[str, Any]:
    """Validate and project host declarations before persistence or publication.

    Unknown keys are dropped, never persisted or proxied. Legacy string rows
    are metadata only. Actual commands are private, including on old hosts.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidCapabilities("host capabilities must be an object")
    _bounded_json(value, MAX_ENVELOPE_BYTES)
    result: dict[str, Any] = {}
    if "host_id" in value:
        if _text(value["host_id"], 256) != host_id:
            raise InvalidCapabilities("capability host identity mismatch")
        result["host_id"] = host_id
    for name, limit in (("display_name", 256), ("kind", 64)):
        if name in value:
            result[name] = _text(value[name], limit)
    if "harnesses" not in value:
        return result
    rows = value["harnesses"]
    if not isinstance(rows, list) or len(rows) > MAX_HARNESSES:
        raise InvalidCapabilities("invalid harness list or too many harnesses")
    public_rows = []
    seen = set()
    for row in rows:
        if isinstance(row, str):
            name = _legacy_identity(row)
            public_row = name
        elif isinstance(row, dict):
            name = (
                _identity(row.get("name"))
                if "capabilities" in row
                else _legacy_identity(row.get("name"))
            )
            enabled = row.get("enabled", False)
            if type(enabled) is not bool:
                raise InvalidCapabilities("harness enabled must be a boolean")
            public_row = {"name": name, "enabled": enabled}
            if "description" in row:
                public_row["description"] = _text(row["description"], 1024)
            if "command" in row:
                command = row["command"]
                if not isinstance(command, list) or len(command) > 64:
                    raise InvalidCapabilities("invalid command metadata")
                for part in command:
                    _text(part, 4096)
                public_row["command"] = []
            if "capabilities" in row:
                matrix = _matrix(row["capabilities"], name)
                public_row["capabilities"] = matrix
                public_row["enabled"] = enabled and bool(matrix["launch_modes"])
        else:
            raise InvalidCapabilities("invalid harness entry")
        if name in seen:
            raise InvalidCapabilities("duplicate harness identity")
        seen.add(name)
        public_rows.append(public_row)
    result["harnesses"] = public_rows
    # Filling missing optional fields can grow a valid input. The persisted
    # projection must fit the same budget when it is read on the next poll.
    _bounded_json(result, MAX_ENVELOPE_BYTES)
    return result


def stored_capabilities(value: str | None, host_id: str) -> dict[str, Any]:
    """Pre-upgrade/corrupt stored data fails closed without breaking the fleet."""
    if not value:
        return {}
    if len(value) > MAX_ENVELOPE_BYTES:
        return {}
    try:
        return validate_capabilities(
            json.loads(value, object_pairs_hook=unique_capability_keys), host_id
        )
    except (ValueError, TypeError, RecursionError):
        return {}
