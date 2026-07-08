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


@lru_cache(maxsize=None)
def load_page(name: str) -> str:
    if name not in _ALLOWED:
        raise FileNotFoundError(f"unknown ui page: {name}")
    return (
        resources.files("drover.server.web")
        .joinpath("static", name)
        .read_text(encoding="utf-8")
    )
