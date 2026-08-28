"""Stable, dependency-free contracts for archive recall."""

from dataclasses import FrozenInstanceError, fields

import pytest

from drover.server.archive import (
    ArchiveMessage,
    ArchiveMessageNeighborhood,
    ArchiveMessageRequest,
    ArchiveDisabled,
    ArchiveError,
    ArchivePartSummary,
    ArchiveProtocolError,
    ArchiveRequestRejected,
    ArchiveResponseTooLarge,
    ArchiveSearchHit,
    ArchiveSearchRequest,
    ArchiveSearchResult,
    ArchiveSession,
    ArchiveStorageUnavailable,
    ArchiveTimeout,
    ArchiveUnavailable,
    SessionArchive,
)


def _part() -> ArchivePartSummary:
    return ArchivePartSummary(
        part_id="part-1", part_type="text", provenance="conversational"
    )


def _message() -> ArchiveMessage:
    return ArchiveMessage(
        message_id="message-1",
        session_id="session-1",
        project="arniesaha/drover",
        source_agent="codex",
        role="user",
        timestamp="2026-08-28T12:00:00Z",
        text="retry state machine",
        parts=(_part(),),
    )


def _hit() -> ArchiveSearchHit:
    return ArchiveSearchHit(
        rank=1,
        message_id="message-1",
        session_id="session-1",
        project="arniesaha/drover",
        source_agent="codex",
        role="user",
        timestamp="2026-08-28T12:00:00Z",
        text="retry state machine",
        score=0.9,
        parts_summary=[_part()],
    )


def test_search_and_neighborhood_collections_are_normalized_to_tuples():
    hit = _hit()
    result = ArchiveSearchResult(
        hits=[hit],
        matched_total=1,
        searchable_in_scope=1,
        has_more=False,
        result_set_freshness="2026-08-28T12:00:00Z",
    )
    neighborhood = ArchiveMessageNeighborhood(
        session=ArchiveSession(
            session_id="session-1",
            project="arniesaha/drover",
            source_agent="codex",
            created_at="2026-08-28T11:00:00Z",
        ),
        target=_message(),
        siblings=[_message()],
        target_part_count=1,
        target_parts_remaining=0,
        context_before=1,
        context_after=0,
    )

    assert isinstance(hit.parts_summary, tuple)
    assert isinstance(result.hits, tuple)
    assert isinstance(neighborhood.siblings, tuple)
    assert isinstance(neighborhood.target.parts, tuple)


def test_session_archive_is_runtime_checkable_and_has_synchronous_operations():
    class FakeArchive:
        def search(self, request):
            return ArchiveSearchResult((), 0, 0, False, None)

        def get_message(self, request):
            raise NotImplementedError

    assert isinstance(FakeArchive(), SessionArchive)


def test_archive_values_are_frozen():
    values = [
        ArchivePartSummary("part-1", "text", "conversational"),
        _message(),
        ArchiveSession(
            "session-1", "arniesaha/drover", "codex", "2026-08-28T11:00:00Z"
        ),
        _hit(),
        ArchiveSearchRequest("retry state machine"),
        ArchiveSearchResult((), 0, 0, False, None),
        ArchiveMessageRequest("message-1"),
        ArchiveMessageNeighborhood(
            ArchiveSession(
                "session-1", "arniesaha/drover", "codex", "2026-08-28T11:00:00Z"
            ),
            _message(),
            (),
            1,
            0,
            0,
            0,
        ),
    ]

    for value in values:
        field_name = fields(value)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(value, field_name, object())


@pytest.mark.parametrize(
    "error_type",
    [
        ArchiveDisabled,
        ArchiveUnavailable,
        ArchiveTimeout,
        ArchiveRequestRejected,
        ArchiveStorageUnavailable,
        ArchiveProtocolError,
        ArchiveResponseTooLarge,
    ],
)
def test_archive_failures_expose_only_sanitized_stable_fields(error_type):
    error = error_type(status_code=503, byte_count=42)

    assert isinstance(error, ArchiveError)
    assert error.category
    assert error.status_code == 503
    assert error.byte_count == 42
    assert str(error) == f"archive {error.category}"
    assert set(vars(error)) == {"category", "status_code", "byte_count"}
    assert "upstream secret" not in str(error_type("upstream secret"))
