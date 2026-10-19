"""Serve the embedded UI pages from package data files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import PurePosixPath
from urllib.parse import unquote

_ALLOWED = {
    "observatory.html",
    "harness.html",
    "harness_terminal.html",
    "history.html",
    "login.html",
}

# Shared scripts are inlined at load time so pages stay single, self-contained
# documents behind the existing auth gate (no new static route to protect).
_INCLUDES = {
    "/*@include harness_capabilities.js*/": "harness_capabilities.js",
    "/*@include history_view.js*/": "history_view.js",
}

_CLIENT_MANIFEST = "client/client-manifest.json"


@dataclass(frozen=True)
class ClientAsset:
    """One browser-visible client file admitted by the build manifest."""

    name: str
    content_type: str
    cache_control: str


def _static(name: str) -> str:
    return (
        resources.files("drover.server.web")
        .joinpath("static", name)
        .read_text(encoding="utf-8")
    )


def _static_bytes(name: str) -> bytes:
    return resources.files("drover.server.web").joinpath("static", name).read_bytes()


def _client_name(value: str) -> str:
    """Return a normalized, relative asset name or reject it before lookup."""

    name = unquote(value)
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or any(part in {".", ".."} for part in path.parts)
        or name != path.as_posix()
    ):
        raise FileNotFoundError("unknown client asset")
    return name


@lru_cache(maxsize=1)
def client_assets() -> dict[str, ClientAsset]:
    """Read the generated allowlist once; never map a URL to package paths."""

    try:
        raw = json.loads(_static(_CLIENT_MANIFEST))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise FileNotFoundError("web client bundle is unavailable") from exc
    if not isinstance(raw, dict):
        raise FileNotFoundError("web client bundle is unavailable")
    assets: dict[str, ClientAsset] = {}
    for raw_name, raw_metadata in raw.items():
        if not isinstance(raw_name, str) or not isinstance(raw_metadata, dict):
            raise FileNotFoundError("web client bundle is unavailable")
        name = _client_name(raw_name)
        content_type = raw_metadata.get("content_type")
        cache_control = raw_metadata.get("cache_control")
        if (
            set(raw_metadata) != {"content_type", "cache_control"}
            or not isinstance(content_type, str)
            or not isinstance(cache_control, str)
        ):
            raise FileNotFoundError("web client bundle is unavailable")
        assets[name] = ClientAsset(name, content_type, cache_control)
    if "index.html" not in assets:
        raise FileNotFoundError("web client bundle is unavailable")
    return assets


def load_client_asset(name: str) -> tuple[ClientAsset, bytes]:
    """Load a manifest-listed client asset without exposing package traversal."""

    asset = client_assets().get(_client_name(name))
    if asset is None:
        raise FileNotFoundError("unknown client asset")
    try:
        return asset, _static_bytes(f"client/{asset.name}")
    except FileNotFoundError as exc:
        raise FileNotFoundError("web client bundle is unavailable") from exc


@lru_cache(maxsize=None)
def load_page(name: str) -> str:
    if name not in _ALLOWED:
        raise FileNotFoundError(f"unknown ui page: {name}")
    page = _static(name)
    for marker, include in _INCLUDES.items():
        if marker in page:
            page = page.replace(marker, _static(include))
    return page
