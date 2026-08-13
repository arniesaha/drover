"""Drover: a local-first cockpit for a personal fleet of CLI coding agents."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

try:
    __version__ = _installed_version("drover")
except PackageNotFoundError:  # pragma: no cover - source tree, never installed
    # A checkout that was never installed still has to report something.
    # This feeds `drover-server --version`, which is the smoke test the
    # installer runs before it will activate a build, so raising here would
    # turn every install into a refusal.
    __version__ = "0.0.0"

__all__ = ["__version__"]
