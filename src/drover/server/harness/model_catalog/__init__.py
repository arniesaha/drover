"""Stable public surface for host-native model discovery."""

from typing import Any, Mapping

from .agy import AgyCatalogAdapter
from .claude import ClaudeCatalogAdapter, ClaudeModelPolicy
from .codex import CodexCatalogAdapter
from .deepseek import DeepSeekCatalogAdapter
from .models import (
    MAX_CATALOG_WIRE_BYTES,
    CatalogEnvelope,
    DiscoveredCatalog,
    ModelOption,
    ReasoningOptions,
    catalog_wire_bytes,
)
from .scope import AccountScopeIDs
from .service import (
    CatalogAdapter,
    CatalogDiscoveryError,
    CatalogSelectionError,
    ModelCatalogService,
)


def default_model_catalog_service(
    host_id: str, presets: Mapping[str, Any]
) -> ModelCatalogService:
    """Build adapters only for enabled presets with resolved executables."""
    adapters: dict[str, CatalogAdapter] = {}
    for harness, adapter_type, suffix in (
        ("codex", CodexCatalogAdapter, ("app-server", "--stdio")),
        ("claude-code", ClaudeCatalogAdapter, ()),
        ("agy", AgyCatalogAdapter, ()),
        ("deepseek-harness", DeepSeekCatalogAdapter, ()),
    ):
        preset = presets.get(harness)
        executable = getattr(preset, "executable", None)
        if (
            preset is None
            or not getattr(preset, "enabled", False)
            or not isinstance(executable, str)
            or not executable
        ):
            continue
        adapters[harness] = adapter_type((executable, *suffix))
    return ModelCatalogService(host_id=host_id, adapters=adapters)


__all__ = [
    "ReasoningOptions",
    "ModelOption",
    "DiscoveredCatalog",
    "CatalogEnvelope",
    "MAX_CATALOG_WIRE_BYTES",
    "catalog_wire_bytes",
    "CatalogDiscoveryError",
    "CatalogSelectionError",
    "CatalogAdapter",
    "AccountScopeIDs",
    "ModelCatalogService",
    "CodexCatalogAdapter",
    "AgyCatalogAdapter",
    "ClaudeCatalogAdapter",
    "ClaudeModelPolicy",
    "DeepSeekCatalogAdapter",
    "default_model_catalog_service",
]
