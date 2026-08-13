"""Drover: a local-first cockpit for a personal fleet of CLI coding agents."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

try:
    __version__ = _installed_version("drover")
except PackageNotFoundError:  # pragma: no cover - source tree, never installed
    # A checkout that was never installed still has to report something, and
    # two callers depend on that. It feeds `drover-server --version`, the
    # smoke test the installer runs before activating a build, so raising
    # here would turn every install into a refusal. It also rides every
    # harnessd heartbeat so the hub can see version skew, so raising would
    # take the daemon down at import time.
    __version__ = "0.0.0"

__all__ = ["__version__"]
