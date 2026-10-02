"""Serve the embedded UI pages from package data files."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

_ALLOWED = {
    "observatory.html",
    "harness.html",
    "harness_terminal.html",
    "login.html",
}

# Shared scripts are inlined at load time so pages stay single, self-contained
# documents behind the existing auth gate (no new static route to protect).
_INCLUDES = {
    "/*@include harness_capabilities.js*/": "harness_capabilities.js",
}


def _static(name: str) -> str:
    return (
        resources.files("drover.server.web")
        .joinpath("static", name)
        .read_text(encoding="utf-8")
    )


@lru_cache(maxsize=None)
def load_page(name: str) -> str:
    if name not in _ALLOWED:
        raise FileNotFoundError(f"unknown ui page: {name}")
    page = _static(name)
    for marker, include in _INCLUDES.items():
        if marker in page:
            page = page.replace(marker, _static(include))
    return page
