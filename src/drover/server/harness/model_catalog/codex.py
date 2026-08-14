"""Native, bounded model discovery through the local Codex CLI."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

from drover.server.providers.codex_app_server import (
    CodexAppServerError,
    CodexAppServerSession,
)

from .agy import _run_bounded
from .models import DiscoveredCatalog, MAX_MODELS, ModelOption, ReasoningOptions
from .service import CatalogDiscoveryError

_CACHE_FILES = ("models_cache.json", "config.toml", "auth.json")
_CONFIGURATION_FILES = ("config.toml", "auth.json")
_MAX_NATIVE_CACHE_BYTES = 1_048_576


class CodexCatalogAdapter:
    """Discover the current Codex model catalog without handling credentials."""

    def __init__(
        self,
        command: Sequence[str],
        codex_home: Path | None = None,
        timeout_s: float = 5.0,
    ):
        self.command = tuple(command)
        self.codex_home = (
            Path(codex_home).expanduser()
            if codex_home is not None
            else Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
        )
        self.timeout_s = timeout_s

    def cache_identity(self) -> str:
        parts = ["command", *self.command]
        for path in (_executable_path(self.command),):
            parts.extend(("executable", str(path), *_stat_metadata(path)))
        for name in _CACHE_FILES:
            path = self.codex_home / name
            parts.extend((name, *_stat_metadata(path)))
        return _fingerprint(parts)

    def _effective_configuration_identity(self) -> str:
        executable = _executable_path(self.command)
        parts = ["command", *self.command, "executable", str(executable)]
        parts.extend(_stat_metadata(executable))
        for name in _CONFIGURATION_FILES:
            path = self.codex_home / name
            parts.extend((name, *_stat_metadata(path)))
        return _fingerprint(parts)

    def discover(self) -> DiscoveredCatalog:
        try:
            return self._discover_live()
        except CatalogDiscoveryError as live_error:
            try:
                return self._discover_native_cache()
            except CatalogDiscoveryError:
                raise live_error

    def _discover_live(self) -> DiscoveredCatalog:
        try:
            with CodexAppServerSession(self.command, self.timeout_s) as client:
                account_response = client.request(
                    "account/read", {"refreshToken": False}
                )
                account_scope_material = _account_scope_material(
                    account_response, self._effective_configuration_identity()
                )
                items = _list_models(client)
            harness_version = self._version()
        except CodexAppServerError as exc:
            raise CatalogDiscoveryError(exc.category) from None
        except CatalogDiscoveryError:
            raise
        except (TypeError, ValueError, OverflowError):
            raise CatalogDiscoveryError("protocol_error") from None

        models = _live_models(items)
        if not models:
            raise CatalogDiscoveryError("protocol_error")
        try:
            return DiscoveredCatalog(
                account_scope_material=account_scope_material,
                harness_version=harness_version,
                models=models,
            )
        except ValueError:
            raise CatalogDiscoveryError("protocol_error") from None

    def _discover_native_cache(self) -> DiscoveredCatalog:
        path = self.codex_home / "models_cache.json"
        try:
            if path.stat().st_size > _MAX_NATIVE_CACHE_BYTES:
                raise ValueError
            with path.open("rb") as stream:
                raw = stream.read(_MAX_NATIVE_CACHE_BYTES + 1)
            if len(raw) > _MAX_NATIVE_CACHE_BYTES:
                raise ValueError
            value = json.loads(raw)
            if not isinstance(value, Mapping):
                raise ValueError
            version = value.get("client_version")
            entries = value.get("models")
            if (
                not isinstance(version, str)
                or not version
                or not isinstance(entries, list)
                or len(entries) > MAX_MODELS
            ):
                raise ValueError
            models = _cached_models(entries)
            if not models:
                raise ValueError
            return DiscoveredCatalog(
                account_scope_material="native-cache:" + self.cache_identity(),
                harness_version=version,
                models=models,
                source_stale=True,
            )
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            raise CatalogDiscoveryError("protocol_error") from None

    def _version(self) -> str:
        if not self.command:
            raise CatalogDiscoveryError("cli_not_found")
        returncode, output = _run_bounded(
            (self.command[0], "--version"),
            timeout_s=self.timeout_s,
            missing_category="cli_not_found",
            os_error_category="unavailable",
        )
        if returncode != 0:
            raise CatalogDiscoveryError("process_error")
        version = output.decode("utf-8", errors="replace").strip()
        if not version:
            raise CatalogDiscoveryError("protocol_error")
        return version


def _list_models(client: CodexAppServerSession) -> list[Mapping[str, Any]]:
    cursor: str | None = None
    items: list[Mapping[str, Any]] = []
    while True:
        page = client.request(
            "model/list", {"cursor": cursor, "includeHidden": False, "limit": 100}
        )
        data = page.get("data")
        if not isinstance(data, list):
            raise CatalogDiscoveryError("protocol_error")
        items.extend(item for item in data if isinstance(item, Mapping))
        cursor = page.get("nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or len(items) > MAX_MODELS:
            raise CatalogDiscoveryError("protocol_error")
    if len(items) > MAX_MODELS:
        raise CatalogDiscoveryError("protocol_error")
    return items


def _account_scope_material(
    response: Mapping[str, Any], configuration_identity: str
) -> str:
    account = response.get("account")
    if not isinstance(account, Mapping):
        raise CatalogDiscoveryError("protocol_error")
    email = account.get("email")
    plan = account.get("planType")
    if not isinstance(email, str) or not email or not isinstance(plan, str) or not plan:
        raise CatalogDiscoveryError("protocol_error")
    return json.dumps(
        {"email": email, "plan": plan, "configuration": configuration_identity},
        separators=(",", ":"),
    )


def _live_models(items: Sequence[Mapping[str, Any]]) -> tuple[ModelOption, ...]:
    return _models_from_items(items, _live_model)


def _cached_models(entries: Sequence[Any]) -> tuple[ModelOption, ...]:
    visible = (
        entry
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("visibility") == "list"
    )
    return _models_from_items(visible, _cached_model)


def _models_from_items(
    items: Sequence[Mapping[str, Any]] | Any,
    convert: Any,
) -> tuple[ModelOption, ...]:
    models: list[ModelOption] = []
    for item in items:
        if len(models) >= MAX_MODELS:
            raise CatalogDiscoveryError("protocol_error")
        try:
            model = convert(item)
        except (TypeError, ValueError):
            continue
        if model is not None:
            models.append(model)
    return tuple(models)


def _live_model(item: Mapping[str, Any]) -> ModelOption | None:
    if item.get("isHidden") is True or item.get("hidden") is True:
        return None
    model_id = item["model"] if "model" in item else item.get("id")
    return _model_option(
        model_id=model_id,
        display_name=item.get("displayName"),
        description=item.get("description"),
        is_default=item.get("isDefault", False),
        default=item.get("defaultReasoningEffort"),
        supported=item.get("supportedReasoningEfforts", []),
        effort_key="reasoningEffort",
    )


def _cached_model(item: Mapping[str, Any]) -> ModelOption:
    return _model_option(
        model_id=item.get("slug"),
        display_name=item.get("display_name"),
        description=item.get("description"),
        is_default=item.get("is_default", False),
        default=item.get("default_reasoning_level"),
        supported=item.get("supported_reasoning_levels", []),
        effort_key="effort",
    )


def _model_option(
    *,
    model_id: Any,
    display_name: Any,
    description: Any,
    is_default: Any,
    default: Any,
    supported: Any,
    effort_key: str,
) -> ModelOption:
    if not isinstance(supported, list):
        raise ValueError
    if default is not None and not isinstance(default, str):
        raise ValueError
    efforts = tuple(
        item[effort_key]
        for item in supported
        if isinstance(item, Mapping) and isinstance(item.get(effort_key), str)
    )
    reasoning = ReasoningOptions(supported=efforts, default=default)
    return ModelOption(
        id=model_id,
        display_name=display_name,
        description=description,
        is_default=is_default,
        reasoning=reasoning,
    )


def _stat_metadata(path: Path) -> tuple[str, ...]:
    try:
        result = path.stat()
    except OSError:
        return ("missing",)
    return (
        "present",
        str(result.st_dev),
        str(result.st_ino),
        str(result.st_mode),
        str(result.st_size),
        str(result.st_mtime_ns),
    )


def _executable_path(command: Sequence[str]) -> Path:
    if not command:
        return Path("")
    executable = command[0]
    path = Path(executable)
    if path.parent == Path("."):
        resolved = shutil.which(executable)
        if resolved:
            return Path(resolved)
    return path


def _fingerprint(parts: Sequence[str]) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()
