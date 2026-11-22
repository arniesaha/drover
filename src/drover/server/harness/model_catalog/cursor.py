"""Bounded, authenticated model discovery through Cursor Agent CLI."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Sequence

from drover.server.harness.auth import strip_ansi

from .agy import _run_bounded
from .models import MAX_MODELS, DiscoveredCatalog, ModelOption
from .service import CatalogDiscoveryError

_MODEL_ROW = re.compile(
    r"^(?P<id>\S+)(?:\s+-\s+(?P<name>.*?))?(?:\s+\((?:current|default)(?:, (?:current|default))*\))?$"
)


class CursorCatalogAdapter:
    """Parse the installed CLI's own ``models`` listing; never invent IDs."""

    def __init__(self, command: Sequence[str], timeout_s: float = 5.0) -> None:
        self.command = tuple(command)
        self.timeout_s = timeout_s

    def cache_identity(self) -> str:
        executable = _executable_path(self.command)
        parts = ["cursor", *self.command, str(executable), *_stat_metadata(executable)]
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()

    def discover(self) -> DiscoveredCatalog:
        _require_executable(self.command)
        if not self._is_authenticated():
            raise CatalogDiscoveryError("not_authenticated")
        returncode, output = _run_bounded(
            [*self.command, "models"],
            timeout_s=self.timeout_s,
            missing_category="unsupported",
        )
        text = output.decode("utf-8", errors="replace")
        if returncode != 0:
            raise CatalogDiscoveryError("protocol_error")
        models = _models(text)
        if not models:
            raise CatalogDiscoveryError("protocol_error")
        version_code, version = _run_bounded(
            [*self.command, "--version"],
            timeout_s=self.timeout_s,
            missing_category="protocol_error",
        )
        version_text = version.decode("utf-8", errors="replace").strip()
        if version_code != 0 or not version_text:
            raise CatalogDiscoveryError("protocol_error")
        try:
            return DiscoveredCatalog(
                # The CLI status command does not expose an account identifier.
                # Keep scope opaque instead of leaking or guessing identity.
                account_scope_material="cursor|authenticated",
                harness_version=version_text,
                models=tuple(models),
            )
        except ValueError:
            raise CatalogDiscoveryError("protocol_error") from None

    def _is_authenticated(self) -> bool:
        returncode, output = _run_bounded(
            [*self.command, "status", "--format", "json"],
            timeout_s=self.timeout_s,
            missing_category="unsupported",
        )
        if returncode != 0:
            return False
        try:
            value = json.loads(output.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            raise CatalogDiscoveryError("protocol_error") from None
        if not isinstance(value, dict):
            raise CatalogDiscoveryError("protocol_error")
        return value.get("isAuthenticated") is True


def _models(text: str) -> list[ModelOption]:
    rows: list[ModelOption] = []
    seen: set[str] = set()
    listing = False
    for raw_line in text.splitlines():
        line = strip_ansi(raw_line).strip()
        if line == "Available models":
            listing = True
            continue
        if not listing:
            continue
        if not line:
            continue
        if line.startswith("Tip:"):
            break
        match = _MODEL_ROW.fullmatch(line)
        if match is None:
            continue
        model_id = match.group("id")
        if model_id in seen:
            continue
        try:
            rows.append(
                ModelOption(
                    id=model_id,
                    display_name=match.group("name") or model_id,
                    is_default="(default)" in line or "(current, default)" in line,
                )
            )
        except ValueError:
            continue
        seen.add(model_id)
        if len(rows) > MAX_MODELS:
            raise CatalogDiscoveryError("protocol_error")
    return rows


def _require_executable(command: Sequence[str]) -> None:
    if not command:
        raise CatalogDiscoveryError("unsupported")
    executable = _executable_path(command)
    try:
        executable.stat()
    except FileNotFoundError:
        raise CatalogDiscoveryError("unsupported") from None
    except OSError:
        raise CatalogDiscoveryError("protocol_error") from None


def _executable_path(command: Sequence[str]) -> Path:
    if not command:
        return Path("")
    candidate = Path(command[0])
    if candidate.parent == Path("."):
        resolved = shutil.which(command[0])
        if resolved:
            return Path(resolved)
    return candidate


def _stat_metadata(path: Path) -> tuple[str, ...]:
    try:
        result = path.stat()
    except OSError:
        return ("missing",)
    return ("present", str(result.st_dev), str(result.st_ino), str(result.st_mtime_ns))
