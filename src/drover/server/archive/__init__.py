"""Stable archive boundary for optional local Pond recall."""

from drover.server.archive.coverage import (
    CoverageReport,
    RegistryCandidate,
    build_coverage_report,
    coverage_summary,
    load_registry_candidates,
)
from drover.server.archive.errors import (
    ArchiveDisabled,
    ArchiveError,
    ArchiveProtocolError,
    ArchiveRequestRejected,
    ArchiveResponseTooLarge,
    ArchiveStorageUnavailable,
    ArchiveTimeout,
    ArchiveUnavailable,
)
from drover.server.archive.inventory import (
    NativeInventory,
    NativeInventoryRecord,
    PondInventory,
    PondInventoryRecord,
    SourceEligibilityReceipt,
    load_native_inventory,
    load_pond_inventory,
    load_source_eligibility_receipt,
    read_private_json,
    write_private_json,
)
from drover.server.archive.native_inventory import (
    discover_native_history_inventory,
    native_inventory_summary,
)
from drover.server.archive.pond import PondArchiveClient
from drover.server.archive.pond_inventory import (
    export_pond_inventory,
    pond_inventory_summary,
)
from drover.server.archive.source_eligibility import (
    assess_metadata_only_source,
    source_eligibility_summary,
)
from drover.server.archive.types import (
    ArchiveMessage,
    ArchiveMessageNeighborhood,
    ArchiveMessageRequest,
    ArchivePartSummary,
    ArchiveSearchHit,
    ArchiveSearchRequest,
    ArchiveSearchResult,
    ArchiveSession,
    SessionArchive,
)

__all__ = [
    "ArchiveDisabled",
    "ArchiveError",
    "ArchiveMessage",
    "ArchiveMessageNeighborhood",
    "ArchiveMessageRequest",
    "ArchivePartSummary",
    "PondArchiveClient",
    "ArchiveProtocolError",
    "ArchiveRequestRejected",
    "ArchiveResponseTooLarge",
    "ArchiveSearchHit",
    "ArchiveSearchRequest",
    "ArchiveSearchResult",
    "ArchiveSession",
    "ArchiveStorageUnavailable",
    "ArchiveTimeout",
    "ArchiveUnavailable",
    "CoverageReport",
    "NativeInventory",
    "NativeInventoryRecord",
    "PondInventory",
    "PondInventoryRecord",
    "SourceEligibilityReceipt",
    "RegistryCandidate",
    "SessionArchive",
    "build_coverage_report",
    "assess_metadata_only_source",
    "coverage_summary",
    "discover_native_history_inventory",
    "export_pond_inventory",
    "load_native_inventory",
    "load_pond_inventory",
    "load_source_eligibility_receipt",
    "load_registry_candidates",
    "native_inventory_summary",
    "pond_inventory_summary",
    "read_private_json",
    "source_eligibility_summary",
    "write_private_json",
]
