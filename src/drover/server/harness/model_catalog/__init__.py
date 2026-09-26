"""Stable public surface for host-native model discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from drover.server.harness.adapters import HarnessAdapterRegistry

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
    host_id: str,
    presets: Mapping[str, Any],
    *,
    adapters: HarnessAdapterRegistry | None = None,
) -> ModelCatalogService:
    """Build adapters only for enabled presets with resolved executables."""
    from drover.server.harness.structured.adapters import BUILTIN_ADAPTERS

    registry = adapters if adapters is not None else BUILTIN_ADAPTERS
    catalog_adapters: dict[str, CatalogAdapter] = {}
    for harness in registry.ids():
        drive_adapter = registry.resolve(harness)
        if not drive_adapter.capabilities.model_catalog:
            continue
        preset = presets.get(harness)
        executable = getattr(preset, "executable", None)
        if (
            preset is None
            or not getattr(preset, "enabled", False)
            or not isinstance(executable, str)
            or not executable
        ):
            continue
        catalog_adapters[harness] = drive_adapter.model_catalog_adapter(executable)
    return ModelCatalogService(host_id=host_id, adapters=catalog_adapters)


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
