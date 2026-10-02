"""Native history inventory and source eligibility utilities."""

from drover.server.archive.inventory import (
    NativeInventory,
    NativeInventoryRecord,
    SourceEligibilityReceipt,
    canonical_private_json_bytes,
    load_native_inventory,
    load_source_eligibility_receipt,
    private_json_sha256,
    read_private_json,
    write_private_json,
)
from drover.server.archive.native_inventory import (
    discover_native_history_inventory,
    native_inventory_summary,
)
from drover.server.archive.source_eligibility import (
    assess_metadata_only_source,
    source_eligibility_summary,
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
    "NativeInventory",
    "NativeInventoryRecord",
    "SourceEligibilityReceipt",
    "canonical_private_json_bytes",
    "load_native_inventory",
    "load_source_eligibility_receipt",
    "private_json_sha256",
    "read_private_json",
    "write_private_json",
    "discover_native_history_inventory",
    "native_inventory_summary",
    "assess_metadata_only_source",
    "source_eligibility_summary",
    "ArchiveDisabled",
    "ArchiveError",
    "ArchiveProtocolError",
    "ArchiveRequestRejected",
    "ArchiveResponseTooLarge",
    "ArchiveStorageUnavailable",
    "ArchiveTimeout",
    "ArchiveUnavailable",
    "ArchiveMessage",
    "ArchiveMessageNeighborhood",
    "ArchiveMessageRequest",
    "ArchivePartSummary",
    "ArchiveSearchHit",
    "ArchiveSearchRequest",
    "ArchiveSearchResult",
    "ArchiveSession",
    "SessionArchive",
]
