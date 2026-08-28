"""Drover-owned, dependency-free types for the session archive boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ArchivePartSummary:
    part_id: str
    part_type: str
    provenance: str


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ArchiveMessage:
    message_id: str
    session_id: str
    project: str
    source_agent: str
    role: str
    timestamp: str
    text: str
    parts: tuple[ArchivePartSummary, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", tuple(self.parts))


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ArchiveSession:
    session_id: str
    project: str
    source_agent: str
    created_at: str
    parent_session_id: str | None = None
    parent_message_id: str | None = None


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ArchiveSearchHit:
    rank: int
    message_id: str
    session_id: str
    project: str
    source_agent: str
    role: str
    timestamp: str
    text: str
    score: float
    parts_summary: tuple[ArchivePartSummary, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts_summary", tuple(self.parts_summary))


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ArchiveSearchRequest:
    query: str
    project: str | None = None
    since: str | None = None
    limit: int = 5


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ArchiveSearchResult:
    hits: tuple[ArchiveSearchHit, ...]
    matched_total: int
    searchable_in_scope: int
    has_more: bool
    result_set_freshness: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "hits", tuple(self.hits))


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ArchiveMessageRequest:
    message_id: str
    context_before: int = 0
    context_after: int = 0


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ArchiveMessageNeighborhood:
    session: ArchiveSession
    target: ArchiveMessage
    siblings: tuple[ArchiveMessage, ...]
    target_part_count: int
    target_parts_remaining: int
    context_before: int
    context_after: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "siblings", tuple(self.siblings))


@runtime_checkable
class SessionArchive(Protocol):
    def search(self, request: ArchiveSearchRequest) -> ArchiveSearchResult: ...

    def get_message(
        self, request: ArchiveMessageRequest
    ) -> ArchiveMessageNeighborhood: ...
