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
    "SessionArchive",
]
