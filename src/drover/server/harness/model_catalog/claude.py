"""Authenticated, policy-aware Claude Code model discovery."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode

from drover.server.providers.claude_credentials import (
    ClaudeCredential,
    ClaudeCredentialError,
    _http_get,
    load_claude_credential,
)

from .agy import _run_bounded
from .models import DiscoveredCatalog, MAX_MODELS, ModelOption, ReasoningOptions
from .service import CatalogDiscoveryError

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_SETTINGS_BYTES = 1024 * 1024
_ALIAS_PATTERN = re.compile(r"^claude-([a-z0-9]+)-")
_PIN_PATTERN = re.compile(r"^ANTHROPIC_DEFAULT_([A-Z0-9]+)_MODEL$")
_PIN_METADATA_PATTERN = re.compile(
    r"^ANTHROPIC_DEFAULT_[A-Z0-9]+_MODEL_(NAME|DESCRIPTION|SUPPORTED_CAPABILITIES)$"
)
_THIRD_PARTY_PROVIDER_KEYS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_PLATFORM",
)
_CUSTOM_MODEL_KEYS = (
    "ANTHROPIC_CUSTOM_MODEL_OPTION",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
)
_AGENTWEAVE_PROXY_URL = "AGENTWEAVE_PROXY_URL"
_AUTHENTICATION_HEADER_NAMES = frozenset(
    {"authorization", "x-api-key", "anthropic-beta"}
)


@dataclass(frozen=True)
class ClaudeModelPolicy:
    available_models: tuple[str, ...] | None
    model_overrides: Mapping[str, str]
    custom_model_id: str | None
    custom_model_name: str | None
    custom_model_description: str | None

    @classmethod
    def load(
        cls, settings_paths: Sequence[str | Path], env: Mapping[str, str]
    ) -> "ClaudeModelPolicy":
        policy, _ = cls._load_with_environment(settings_paths, env)
        return policy

    @classmethod
    def _load_with_environment(
        cls, settings_paths: Sequence[str | Path], env: Mapping[str, str]
    ) -> tuple["ClaudeModelPolicy", dict[str, str]]:
        available: list[str] | None = None
        seen_available: set[str] = set()
        overrides: dict[str, str] = {}
        settings_env: dict[str, str] = {}

        for value in settings_paths:
            path = Path(value).expanduser()
            try:
                if path.stat().st_size > _MAX_SETTINGS_BYTES:
                    raise ValueError
                raw = path.read_bytes()
            except FileNotFoundError:
                continue
            except OSError:
                # Production uses the readable subset of the documented
                # paths. An unreadable optional source is not effective
                # configuration for this process.
                continue
            if len(raw) > _MAX_SETTINGS_BYTES:
                raise ValueError
            try:
                settings = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError from None
            if not isinstance(settings, Mapping):
                raise ValueError

            if "availableModels" in settings:
                entries = settings["availableModels"]
                if not isinstance(entries, list):
                    raise ValueError
                if available is None:
                    available = []
                for entry in entries:
                    if not _valid_text(entry):
                        raise ValueError
                    if entry not in seen_available:
                        seen_available.add(entry)
                        available.append(entry)

            if "modelOverrides" in settings:
                value_overrides = settings["modelOverrides"]
                if not isinstance(value_overrides, Mapping):
                    raise ValueError
                for model_id, provider_id in value_overrides.items():
                    if not _valid_text(model_id) or not _valid_text(provider_id):
                        raise ValueError
                    overrides[model_id] = provider_id

            if "env" in settings:
                value_env = settings["env"]
                if not isinstance(value_env, Mapping):
                    raise ValueError
                for key, value in value_env.items():
                    if not _valid_text(key) or not isinstance(value, str):
                        raise ValueError
                    settings_env[key] = value

        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in env.items()
        ):
            raise ValueError
        effective_env = {**settings_env, **env}
        return (
            cls(
                available_models=tuple(available) if available is not None else None,
                model_overrides=MappingProxyType(overrides),
                custom_model_id=_env_text(effective_env, _CUSTOM_MODEL_KEYS[0]),
                custom_model_name=_env_text(effective_env, _CUSTOM_MODEL_KEYS[1]),
                custom_model_description=_env_text(
                    effective_env, _CUSTOM_MODEL_KEYS[2]
                ),
            ),
            effective_env,
        )


class ClaudeCatalogAdapter:
    """Discover the models selectable by this host's Claude Code process."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        credential_loader: Callable[[], ClaudeCredential] | None = None,
        opener: Callable[[str, dict[str, str], float], tuple[int, bytes]] | None = None,
        settings_paths: Sequence[str | Path] | None = None,
        env: Mapping[str, str] | None = None,
        version_reader: Callable[[Sequence[str]], str] | None = None,
        account_path: str | Path | None = None,
        credentials_path: str | Path | None = None,
        timeout_s: float = 5.0,
    ):
        self.command = tuple(command)
        self.account_path = (
            Path(account_path).expanduser()
            if account_path is not None
            else Path.home() / ".claude.json"
        )
        self.credentials_path = (
            Path(credentials_path).expanduser()
            if credentials_path is not None
            else Path.home() / ".claude" / ".credentials.json"
        )
        self.settings_paths = tuple(
            Path(path).expanduser()
            for path in (
                settings_paths
                if settings_paths is not None
                else (
                    Path.home() / ".claude/settings.json",
                    Path.home() / ".claude/settings.local.json",
                    Path(
                        "/Library/Application Support/ClaudeCode/managed-settings.json"
                    ),
                    Path("/etc/claude-code/managed-settings.json"),
                )
            )
        )
        self.env = dict(os.environ if env is None else env)
        self.timeout_s = timeout_s
        self.opener = opener or _http_get
        self.credential_loader = credential_loader or (
            lambda: load_claude_credential(
                credentials_path=self.credentials_path,
                account_path=self.account_path,
            )
        )
        self.version_reader = version_reader or (
            lambda command: _read_version(command, timeout_s=self.timeout_s)
        )

    def cache_identity(self) -> str:
        executable = _executable_path(self.command)
        parts = ["command", *self.command, "executable", str(executable)]
        parts.extend(_stat_metadata(executable))
        for path in self.settings_paths:
            parts.extend(("settings", str(path), *_stat_metadata(path)))
        parts.extend(
            ("account", str(self.account_path), *_stat_metadata(self.account_path))
        )
        parts.extend(
            (
                "credentials",
                str(self.credentials_path),
                *_stat_metadata(self.credentials_path),
            )
        )
        parts.extend(("base_url", self._base_url(self.env)))
        parts.extend(
            (
                "custom_headers",
                "configured" if _has_custom_headers(self.env) else "absent",
            )
        )
        if _agentweave_proxy_url(self.env) is not None and _has_custom_headers(
            self.env
        ):
            parts.extend(
                ("agentweave_custom_headers", _custom_headers_fingerprint(self.env))
            )
        for key in sorted(self.env):
            if _is_non_secret_model_key(key):
                parts.extend((key, self.env[key]))
        return _fingerprint(parts)

    def discover(self) -> DiscoveredCatalog:
        try:
            policy, effective_env = ClaudeModelPolicy._load_with_environment(
                self.settings_paths, self.env
            )
        except (TypeError, ValueError, OverflowError):
            raise CatalogDiscoveryError("protocol_error") from None

        agentweave_proxy_url = _agentweave_proxy_url(effective_env)
        if agentweave_proxy_url is not None and _has_custom_headers(effective_env):
            custom_headers = _custom_headers(effective_env)
            headers, account_scope_material = self._authentication(
                effective_env, agentweave_proxy_url
            )
            headers.update(custom_headers)
            account_scope_material = f"{account_scope_material}\0{_custom_headers_fingerprint(effective_env)}"
            items = self._models(
                headers, agentweave_proxy_url, allow_missing_pagination=True
            )
            version = self._version()
            try:
                models = _model_options(items, policy, effective_env)
                return DiscoveredCatalog(
                    account_scope_material=account_scope_material,
                    harness_version=version,
                    models=models,
                )
            except (TypeError, ValueError, OverflowError):
                raise CatalogDiscoveryError("protocol_error") from None

        if _has_custom_headers(effective_env):
            raise CatalogDiscoveryError("unsupported")

        if any(_truthy(effective_env.get(key)) for key in _THIRD_PARTY_PROVIDER_KEYS):
            # The direct Models API cannot authoritatively enumerate these
            # provider-native inventories. A partial Anthropic list would be
            # worse than the service's stale/default behavior.
            raise CatalogDiscoveryError("unsupported")

        base_url = self._base_url(effective_env)
        headers, account_scope_material = self._authentication(effective_env, base_url)
        items = self._models(headers, base_url)
        version = self._version()
        try:
            models = _model_options(items, policy, effective_env)
            return DiscoveredCatalog(
                account_scope_material=account_scope_material,
                harness_version=version,
                models=models,
            )
        except (TypeError, ValueError, OverflowError):
            raise CatalogDiscoveryError("protocol_error") from None

    def _authentication(
        self, env: Mapping[str, str], base_url: str
    ) -> tuple[dict[str, str], str]:
        headers = {
            "Accept": "application/json",
            "anthropic-version": "2023-06-01",
        }
        api_key = env.get("ANTHROPIC_API_KEY")
        if isinstance(api_key, str) and api_key:
            headers["x-api-key"] = api_key
            return headers, f"{api_key}\0{base_url}"

        try:
            credential = self.credential_loader()
        except ClaudeCredentialError as exc:
            category = (
                "not_authenticated"
                if exc.category in {"not_authenticated", "token_expired"}
                else "protocol_error"
            )
            raise CatalogDiscoveryError(category) from None
        except (TypeError, ValueError, OverflowError, OSError):
            raise CatalogDiscoveryError("protocol_error") from None
        if not isinstance(credential, ClaudeCredential):
            raise CatalogDiscoveryError("protocol_error")
        headers["Authorization"] = f"Bearer {credential.access_token}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
        return headers, f"{credential.account_identity}\0{base_url}"

    def _models(
        self,
        headers: dict[str, str],
        base_url: str,
        *,
        allow_missing_pagination: bool = False,
    ) -> list[Mapping[str, Any]]:
        after_id: str | None = None
        seen_cursors: set[str] = set()
        items: list[Mapping[str, Any]] = []
        total_bytes = 0
        raw_count = 0
        while True:
            query = {"limit": "1000"}
            if after_id is not None:
                query["after_id"] = after_id
            url = f"{base_url}/v1/models?{urlencode(query)}"
            try:
                status, body = self.opener(url, headers, self.timeout_s)
            except TimeoutError:
                raise CatalogDiscoveryError("timeout") from None
            except (http.client.HTTPException, OSError):
                raise CatalogDiscoveryError("offline") from None
            except Exception:
                raise CatalogDiscoveryError("protocol_error") from None

            if not isinstance(status, int) or isinstance(status, bool):
                raise CatalogDiscoveryError("protocol_error")
            if status in (401, 403):
                raise CatalogDiscoveryError("not_authenticated")
            if status < 200 or status >= 300:
                raise CatalogDiscoveryError("offline")
            if not isinstance(body, bytes) or len(body) > _MAX_RESPONSE_BYTES:
                raise CatalogDiscoveryError("protocol_error")
            total_bytes += len(body)
            if total_bytes > _MAX_RESPONSE_BYTES:
                raise CatalogDiscoveryError("protocol_error")
            try:
                page = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise CatalogDiscoveryError("protocol_error") from None
            if not isinstance(page, Mapping) or not isinstance(page.get("data"), list):
                raise CatalogDiscoveryError("protocol_error")
            data = page["data"]
            raw_count += len(data)
            if raw_count > MAX_MODELS:
                raise CatalogDiscoveryError("protocol_error")
            items.extend(item for item in data if isinstance(item, Mapping))

            has_more = page.get("has_more")
            if "has_more" not in page and allow_missing_pagination:
                return items
            if not isinstance(has_more, bool):
                raise CatalogDiscoveryError("protocol_error")
            if not has_more:
                return items
            last_id = page.get("last_id")
            if not _valid_text(last_id) or last_id in seen_cursors or not data:
                raise CatalogDiscoveryError("protocol_error")
            seen_cursors.add(last_id)
            after_id = last_id

    def _version(self) -> str:
        try:
            value = self.version_reader(self.command)
        except Exception:
            raise CatalogDiscoveryError("protocol_error") from None
        if not _valid_text(value):
            raise CatalogDiscoveryError("protocol_error")
        return value

    @staticmethod
    def _base_url(env: Mapping[str, str]) -> str:
        value = env.get("ANTHROPIC_BASE_URL")
        if not isinstance(value, str) or not value.strip():
            return _DEFAULT_BASE_URL
        return value.rstrip("/")


def _model_options(
    items: Sequence[Mapping[str, Any]],
    policy: ClaudeModelPolicy,
    env: Mapping[str, str],
) -> tuple[ModelOption, ...]:
    reverse_overrides = {
        provider_id: model_id
        for model_id, provider_id in policy.model_overrides.items()
    }
    models: list[ModelOption] = []
    seen_ids: set[str] = set()

    for item in items:
        provider_id = item.get("id")
        if not _valid_text(provider_id):
            continue
        selectable_id = reverse_overrides.get(provider_id, provider_id)
        alias = _alias_for(provider_id)
        output_id = _policy_selection(
            selectable_id,
            provider_id,
            alias,
            policy.available_models,
        )
        if output_id is None or output_id in seen_ids:
            continue
        try:
            model = _provider_model(item, output_id)
        except (TypeError, ValueError):
            continue
        seen_ids.add(output_id)
        models.append(model)
        if len(models) > MAX_MODELS:
            raise ValueError

    for model in _pinned_models(env, policy.available_models):
        if model.id not in seen_ids:
            seen_ids.add(model.id)
            models.append(model)
            if len(models) > MAX_MODELS:
                raise ValueError

    if policy.custom_model_id is not None and _allowed_exact(
        policy.custom_model_id, policy.available_models
    ):
        if policy.custom_model_id not in seen_ids:
            models.append(
                ModelOption(
                    id=policy.custom_model_id,
                    display_name=policy.custom_model_name or policy.custom_model_id,
                    description=(
                        policy.custom_model_description
                        or f"Custom model ({policy.custom_model_id})"
                    ),
                )
            )

    if not models:
        raise ValueError
    return tuple(models)


def _provider_model(item: Mapping[str, Any], output_id: str) -> ModelOption:
    display_name = item.get("display_name")
    if display_name is None:
        display_name = output_id
    elif not _valid_text(display_name):
        raise ValueError
    description = item.get("description")
    if description is not None and not _valid_text(description):
        raise ValueError
    supported = item.get("supported_efforts")
    default = item.get("default_effort")
    reasoning: ReasoningOptions | None = None
    if isinstance(supported, list) and _valid_text(default):
        if not all(_valid_text(effort) for effort in supported):
            raise ValueError
        reasoning = ReasoningOptions(supported=tuple(supported), default=default)
    return ModelOption(
        id=output_id,
        display_name=display_name,
        description=description,
        reasoning=reasoning,
    )


def _policy_selection(
    selectable_id: str,
    provider_id: str,
    alias: str | None,
    available: tuple[str, ...] | None,
) -> str | None:
    if available is None:
        return alias or selectable_id
    for allowed in available:
        normalized_allowed = _without_context_suffix(allowed)
        if alias is not None and normalized_allowed == alias:
            return allowed
        if _matches_full_model(normalized_allowed, selectable_id):
            return selectable_id
        if _matches_full_model(normalized_allowed, provider_id):
            return selectable_id
    return None


def _matches_full_model(allowed: str, candidate: str) -> bool:
    candidate = _without_context_suffix(candidate)
    return candidate == allowed or candidate.startswith(f"{allowed}-")


def _allowed_exact(model_id: str, available: tuple[str, ...] | None) -> bool:
    if available is None:
        return True
    normalized = _without_context_suffix(model_id)
    return any(_without_context_suffix(item) == normalized for item in available)


def _pinned_models(
    env: Mapping[str, str], available: tuple[str, ...] | None
) -> tuple[ModelOption, ...]:
    models: list[ModelOption] = []
    for key, raw_model_id in env.items():
        match = _PIN_PATTERN.fullmatch(key)
        if match is None or not _valid_text(raw_model_id):
            continue
        family = match.group(1).lower()
        if available is not None and not (
            _allowed_exact(raw_model_id, available) or family in available
        ):
            continue
        name = _env_text(env, f"{key}_NAME") or raw_model_id
        description = _env_text(env, f"{key}_DESCRIPTION")
        models.append(
            ModelOption(
                id=raw_model_id,
                display_name=name,
                description=description,
            )
        )
    return tuple(models)


def _alias_for(model_id: str) -> str | None:
    match = _ALIAS_PATTERN.match(model_id)
    return match.group(1) if match is not None else None


def _without_context_suffix(value: str) -> str:
    return value[:-4] if value.endswith("[1m]") else value


def _env_text(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    return value if _valid_text(value) else None


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _truthy(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() not in {
        "",
        "0",
        "false",
        "no",
        "off",
    }


def _has_custom_headers(env: Mapping[str, str]) -> bool:
    value = env.get("ANTHROPIC_CUSTOM_HEADERS")
    return isinstance(value, str) and bool(value)


def _agentweave_proxy_url(env: Mapping[str, str]) -> str | None:
    value = env.get(_AGENTWEAVE_PROXY_URL)
    if not _valid_text(value):
        return None
    return value.rstrip("/")


def _custom_headers(env: Mapping[str, str]) -> dict[str, str]:
    value = env.get("ANTHROPIC_CUSTOM_HEADERS")
    if not isinstance(value, str) or not value:
        raise CatalogDiscoveryError("protocol_error")

    headers: dict[str, str] = {}
    for line in value.splitlines():
        name, separator, header_value = line.partition(":")
        name = name.strip()
        if not separator or not name or name.lower() in _AUTHENTICATION_HEADER_NAMES:
            raise CatalogDiscoveryError("protocol_error")
        headers[name] = header_value.lstrip()
    return headers


def _custom_headers_fingerprint(env: Mapping[str, str]) -> str:
    value = env.get("ANTHROPIC_CUSTOM_HEADERS")
    if not isinstance(value, str) or not value:
        raise ValueError
    return hashlib.sha256(value.encode()).hexdigest()


def _is_non_secret_model_key(key: str) -> bool:
    if key in _THIRD_PARTY_PROVIDER_KEYS or key in _CUSTOM_MODEL_KEYS:
        return True
    if key == "ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES":
        return True
    if key in {
        "ANTHROPIC_MODEL",
        _AGENTWEAVE_PROXY_URL,
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
        "CLAUDE_CODE_DISABLE_1M_CONTEXT",
    }:
        return True
    return (
        _PIN_PATTERN.fullmatch(key) is not None
        or _PIN_METADATA_PATTERN.fullmatch(key) is not None
    )


def _read_version(command: Sequence[str], *, timeout_s: float) -> str:
    if not command:
        raise ValueError
    returncode, output = _run_bounded(
        (*command, "--version"),
        timeout_s=timeout_s,
        missing_category="protocol_error",
    )
    if returncode != 0:
        raise ValueError
    return output.decode("utf-8", errors="replace").strip()


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
    path = Path(command[0])
    if path.parent == Path("."):
        resolved = shutil.which(command[0])
        if resolved:
            return Path(resolved)
    return path


def _fingerprint(parts: Sequence[str]) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()
