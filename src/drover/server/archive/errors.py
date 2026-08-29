"""Sanitized failures raised by archive implementations."""

from __future__ import annotations


class ArchiveError(RuntimeError):
    """Base class for failures crossing the archive boundary.

    Only the category and optional transport measurements are retained. In
    particular, upstream response text is intentionally never stored here.
    """

    category = "archive_error"

    def __init__(
        self, status_code: int | None = None, byte_count: int | None = None
    ) -> None:
        self.category = type(self).category
        self.status_code = status_code if type(status_code) is int else None
        self.byte_count = byte_count if type(byte_count) is int else None
        super().__init__()

    def __str__(self) -> str:
        return f"archive {self.category}"


class ArchiveDisabled(ArchiveError):
    category = "disabled"


class ArchiveUnavailable(ArchiveError):
    category = "unavailable"


class ArchiveTimeout(ArchiveError):
    category = "timeout"


class ArchiveRequestRejected(ArchiveError):
    category = "request_rejected"


class ArchiveStorageUnavailable(ArchiveError):
    category = "storage_unavailable"


class ArchiveProtocolError(ArchiveError):
    category = "protocol_error"


class ArchiveResponseTooLarge(ArchiveError):
    category = "response_too_large"
