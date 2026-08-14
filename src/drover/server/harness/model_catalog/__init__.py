"""Stable public surface for host-native model discovery."""

from .models import CatalogEnvelope, DiscoveredCatalog, ModelOption, ReasoningOptions
from .codex import CodexCatalogAdapter
from .agy import AgyCatalogAdapter
from .claude import ClaudeCatalogAdapter, ClaudeModelPolicy
from .scope import AccountScopeIDs
from .service import (
    CatalogAdapter,
    CatalogDiscoveryError,
    CatalogSelectionError,
    ModelCatalogService,
)

__all__ = [
    "ReasoningOptions",
    "ModelOption",
    "DiscoveredCatalog",
    "CatalogEnvelope",
    "CatalogDiscoveryError",
    "CatalogSelectionError",
    "CatalogAdapter",
    "AccountScopeIDs",
    "ModelCatalogService",
    "CodexCatalogAdapter",
    "AgyCatalogAdapter",
    "ClaudeCatalogAdapter",
    "ClaudeModelPolicy",
]
