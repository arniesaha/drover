"""Stable archive boundary for optional local Pond recall."""

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
    load_native_inventory,
    load_pond_inventory,
    read_private_json,
    write_private_json,
)
from drover.server.archive.pond import PondArchiveClient
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
    "NativeInventory",
    "NativeInventoryRecord",
    "PondInventory",
    "PondInventoryRecord",
    "SessionArchive",
    "load_native_inventory",
    "load_pond_inventory",
    "read_private_json",
    "write_private_json",
]
