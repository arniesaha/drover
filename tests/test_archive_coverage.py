"""Candidate archive coverage and conservative readiness gates."""

from __future__ import annotations

import json

import duckdb
import pytest

from drover.server.archive.coverage import (
    RegistryCandidate,
    build_coverage_report,
    coverage_summary,
    load_registry_candidates,
)
from drover.server.archive.inventory import (
    NativeInventory,
    NativeInventoryRecord,
    PondInventory,
    PondInventoryRecord,
    SourceEligibilityReceipt,
)

_CAPTURED_AT = "2026-08-28T12:00:00Z"


def _source(
    source_agent: str,
    session_id: str,
    *,
    source_copies: int = 1,
) -> NativeInventoryRecord:
    return NativeInventoryRecord(
        source_agent=source_agent,
        session_id=session_id,
        updated_at="2026-08-28T11:00:00Z",
        size_bytes=123,
        source_copies=source_copies,
    )


def _native(host_id: str, *records: NativeInventoryRecord) -> NativeInventory:
    return NativeInventory(1, _CAPTURED_AT, host_id, tuple(records))


def _archive(
    session_id: str,
    source_agent: str,
    *,
    created_at: str = "2026-08-28T10:00:00Z",
    message_count: int = 1,
    first_message_at: str | None = "2026-08-28T10:01:00Z",
    last_message_at: str | None = "2026-08-28T10:02:00Z",
) -> PondInventoryRecord:
    return PondInventoryRecord(
        session_id=session_id,
        source_agent=source_agent,
        created_at=created_at,
        message_count=message_count,
        first_message_at=first_message_at,
        last_message_at=last_message_at,
    )


def _pond(*records: PondInventoryRecord) -> PondInventory:
    return PondInventory(1, _CAPTURED_AT, "0.16.3", tuple(records))


def _candidate(
    session_id: str,
    host_id: str,
    harness: str,
    native_session_id: str,
) -> RegistryCandidate:
    return RegistryCandidate(session_id, host_id, harness, native_session_id)


def _receipt(
    host_id: str,
    session_id: str,
    fingerprint: str,
) -> SourceEligibilityReceipt:
    return SourceEligibilityReceipt(
        schema_version=1,
        assessed_at=_CAPTURED_AT,
        host_id=host_id,
        source_agent="claude-code",
        session_id=session_id,
        source_fingerprint=fingerprint,
        classification="source_not_archive_eligible",
    )


def test_registry_projection_filters_blank_ids_preserves_wrappers_and_is_read_only(
    tmp_path, monkeypatch
):
    registry_path = tmp_path / "control-plane-copy.duckdb"
    with duckdb.connect(str(registry_path)) as connection:
        connection.execute("""
            CREATE TABLE harness_sessions (
                session_id VARCHAR,
                host_id VARCHAR,
                harness VARCHAR,
                native_session_id VARCHAR,
                transcript_preview VARCHAR
            )
            """)
        connection.executemany(
            "INSERT INTO harness_sessions VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "wrapper-b",
                    "host-b",
                    "codex",
                    "native-shared",
                    "SENSITIVE-CANARY-TRANSCRIPT",
                ),
                ("blank", "host-a", "codex", "  ", "blank-canary"),
                (
                    "unsupported",
                    "host-a",
                    "gemini",
                    "native-unsupported",
                    "unsupported-canary",
                ),
                (
                    "wrapper-a2",
                    "host-a",
                    "codex",
                    "native-shared",
                    "wrapper-a2-canary",
                ),
                (
                    "wrapper-a1",
                    "host-a",
                    "codex",
                    "native-shared",
                    "wrapper-a1-canary",
                ),
                ("missing", "host-a", "claude-code", None, "missing-canary"),
            ],
        )
    real_connect = duckdb.connect
    connect_calls = []

    def recording_connect(*args, **kwargs):
        connect_calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(
        "drover.server.archive.coverage.duckdb.connect", recording_connect
    )

    candidates = load_registry_candidates(registry_path)

    assert candidates == (
        RegistryCandidate("wrapper-a1", "host-a", "codex", "native-shared"),
        RegistryCandidate("wrapper-a2", "host-a", "codex", "native-shared"),
        RegistryCandidate("unsupported", "host-a", "gemini", "native-unsupported"),
        RegistryCandidate("wrapper-b", "host-b", "codex", "native-shared"),
    )
    assert "SENSITIVE-CANARY-TRANSCRIPT" not in repr(candidates)
    assert connect_calls == [((str(registry_path),), {"read_only": True})]
    with duckdb.connect(str(registry_path), read_only=True) as connection:
        assert connection.execute("SHOW TABLES").fetchall() == [("harness_sessions",)]


def test_candidate_rows_receive_all_four_states_and_public_aggregate_shape():
    registry = (
        _candidate("drover-1", "host-a", "claude-code", "native-matched"),
        _candidate("drover-2", "host-a", "codex", "native-current"),
        _candidate("drover-3", "host-old", "codex", "native-prior"),
        _candidate("drover-4", "host-z", "claude-code", "native-unknown"),
    )
    current = (
        _native(
            "host-a",
            _source("claude-code", "native-matched"),
            _source("codex-cli", "native-current"),
            _source("claude-code", "source-only-a"),
            _source("codex-cli", "source-only-b"),
            _source("codex-cli", "source-only-c"),
        ),
    )
    prior = (_native("host-old", _source("codex-cli", "native-prior")),)
    pond = _pond(
        _archive("native-matched", "claude-code", created_at="2026-08-28T01:00:00Z"),
        _archive("source-only-a", "claude-code", created_at="2026-08-28T02:00:00Z"),
        _archive("source-only-b", "codex-cli", created_at="2026-08-28T03:00:00Z"),
        _archive("source-only-c", "codex-cli", created_at="2026-08-28T04:00:00Z"),
    )

    report = build_coverage_report(registry, current, pond, prior_sources=prior)

    assert [detail.status for detail in report.details] == [
        "matched",
        "discovered_not_synced",
        "source_absent_after_prior_inventory",
        "unverifiable",
    ]
    assert report.to_wire()["certified_coverage"] == {
        "status": "not_implemented",
        "certified": 0,
    }
    assert coverage_summary(report) == {
        "schema_version": 1,
        "candidate_coverage": {
            "eligible": 4,
            "matched": 1,
            "percent": 25.0,
            "by_harness": {
                "claude-code": {"eligible": 2, "matched": 1},
                "codex-cli": {"eligible": 2, "matched": 0},
            },
        },
        "current_source_coverage": {
            "discovered": 5,
            "matched": 4,
            "source_not_archive_eligible": 0,
            "discovered_not_synced": 1,
        },
        "certified_coverage": {"status": "not_implemented", "certified": 0},
        "misses": {
            "discovered_not_synced": 1,
            "source_absent_after_prior_inventory": 1,
            "unverifiable": 1,
        },
        "collisions": {
            "duplicate_source_groups": 0,
            "cross_harness_native_id_groups": 0,
            "archive_logical_duplicate_candidate_groups": 0,
            "archive_signature_unverifiable": 0,
        },
        "unsupported_harness_sessions": 0,
        "ready_for_next_writer": False,
    }


def test_repeated_drover_wrappers_are_rows_not_source_duplicates():
    registry = (
        _candidate("drover-a", "host-a", "codex", "native-shared"),
        _candidate("drover-b", "host-a", "codex", "native-shared"),
    )
    pond = _pond(_archive("native-shared", "codex-cli"))

    summary = coverage_summary(build_coverage_report(registry, (), pond))

    assert summary["candidate_coverage"] == {
        "eligible": 2,
        "matched": 2,
        "percent": 100.0,
        "by_harness": {"codex-cli": {"eligible": 2, "matched": 2}},
    }
    assert summary["collisions"]["duplicate_source_groups"] == 0
    assert summary["ready_for_next_writer"] is True


def test_unsupported_registry_harnesses_are_counted_but_never_become_summary_keys():
    registry = (
        _candidate("drover-codex", "host-a", "codex", "native-codex"),
        _candidate("drover-claude", "host-a", "claude-code", "native-claude"),
        _candidate("drover-other", "host-a", "private-agent", "native-other"),
    )
    pond = _pond(
        _archive("native-codex", "codex-cli"),
        _archive("native-claude", "claude-code", created_at="2026-08-28T11:00:00Z"),
    )

    summary = coverage_summary(build_coverage_report(registry, (), pond))

    assert summary["unsupported_harness_sessions"] == 1
    assert summary["candidate_coverage"]["eligible"] == 2
    assert summary["candidate_coverage"]["by_harness"] == {
        "claude-code": {"eligible": 1, "matched": 1},
        "codex-cli": {"eligible": 1, "matched": 1},
    }
    assert "private-agent" not in json.dumps(summary)


def test_current_sources_without_registry_rows_still_block_progression():
    current = (
        _native(
            "host-a",
            _source("codex-cli", "source-matched"),
            _source("claude-code", "source-never-launched"),
        ),
    )
    pond = _pond(_archive("source-matched", "codex-cli"))

    summary = coverage_summary(build_coverage_report((), current, pond))

    assert summary["candidate_coverage"]["eligible"] == 0
    assert summary["candidate_coverage"]["percent"] == 0.0
    assert summary["current_source_coverage"] == {
        "discovered": 2,
        "matched": 1,
        "source_not_archive_eligible": 0,
        "discovered_not_synced": 1,
    }
    assert summary["ready_for_next_writer"] is False


def test_matching_receipt_classifies_metadata_only_source_without_claiming_archive_match():
    fingerprint = "a" * 64
    current = (
        NativeInventory(
            2,
            _CAPTURED_AT,
            "host-a",
            (
                NativeInventoryRecord(
                    "claude-code",
                    "metadata-only",
                    "2026-08-28T11:00:00Z",
                    123,
                    1,
                    fingerprint,
                ),
            ),
        ),
    )

    report = build_coverage_report(
        (),
        current,
        _pond(),
        eligibility_receipts=(_receipt("host-a", "metadata-only", fingerprint),),
    )
    summary = coverage_summary(report)

    assert report.current_source_details[0].status == "source_not_archive_eligible"
    assert summary["current_source_coverage"] == {
        "discovered": 1,
        "matched": 0,
        "source_not_archive_eligible": 1,
        "discovered_not_synced": 0,
    }
    assert summary["ready_for_next_writer"] is True


@pytest.mark.parametrize(
    "receipt_factory",
    [
        lambda fingerprint: _receipt("other-host", "metadata-only", fingerprint),
        lambda _fingerprint: _receipt("host-a", "metadata-only", "b" * 64),
    ],
    ids=["host-replay", "changed-source"],
)
def test_receipt_must_match_the_current_host_source_and_fingerprint(receipt_factory):
    fingerprint = "a" * 64
    current = (
        NativeInventory(
            2,
            _CAPTURED_AT,
            "host-a",
            (
                NativeInventoryRecord(
                    "claude-code",
                    "metadata-only",
                    "2026-08-28T11:00:00Z",
                    123,
                    1,
                    fingerprint,
                ),
            ),
        ),
    )

    with pytest.raises(
        ValueError, match=r"^archive coverage eligibility receipt$"
    ) as raised:
        build_coverage_report(
            (),
            current,
            _pond(),
            eligibility_receipts=(receipt_factory(fingerprint),),
        )

    assert "metadata-only" not in str(raised.value)
    assert "host-a" not in str(raised.value)
    assert fingerprint not in str(raised.value)


def test_receipt_refuses_duplicate_source_copy_or_existing_archive_session():
    fingerprint = "a" * 64
    receipt = _receipt("host-a", "metadata-only", fingerprint)
    duplicated = (
        NativeInventory(
            2,
            _CAPTURED_AT,
            "host-a",
            (
                NativeInventoryRecord(
                    "claude-code",
                    "metadata-only",
                    "2026-08-28T11:00:00Z",
                    123,
                    2,
                    fingerprint,
                ),
            ),
        ),
    )
    single = (
        NativeInventory(
            2,
            _CAPTURED_AT,
            "host-a",
            (
                NativeInventoryRecord(
                    "claude-code",
                    "metadata-only",
                    "2026-08-28T11:00:00Z",
                    123,
                    1,
                    fingerprint,
                ),
            ),
        ),
    )
    cross_host_duplicate = (
        *single,
        NativeInventory(
            2,
            _CAPTURED_AT,
            "host-b",
            (
                NativeInventoryRecord(
                    "claude-code",
                    "metadata-only",
                    "2026-08-28T11:00:00Z",
                    123,
                    1,
                    "b" * 64,
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="eligibility receipt"):
        build_coverage_report((), duplicated, _pond(), eligibility_receipts=(receipt,))
    with pytest.raises(ValueError, match="eligibility receipt"):
        build_coverage_report(
            (),
            cross_host_duplicate,
            _pond(),
            eligibility_receipts=(receipt,),
        )
    with pytest.raises(ValueError, match="eligibility receipt"):
        build_coverage_report(
            (),
            single,
            _pond(_archive("metadata-only", "claude-code")),
            eligibility_receipts=(receipt,),
        )


def test_current_source_manifests_require_one_snapshot_per_host():
    current = (
        _native("host-a", _source("codex-cli", "native-a")),
        _native("host-a", _source("codex-cli", "native-b")),
    )

    with pytest.raises(ValueError, match="current source inventory") as raised:
        build_coverage_report((), current, _pond())

    assert "host-a" not in str(raised.value)


def test_prior_manifests_allow_older_snapshots_but_cannot_certify_coverage():
    registry = (_candidate("drover-prior", "host-old", "codex", "native-prior"),)
    prior = (
        _native("host-old", _source("codex-cli", "native-older")),
        _native("host-old", _source("codex-cli", "native-prior")),
    )

    report = build_coverage_report(registry, (), _pond(), prior_sources=prior)
    summary = coverage_summary(report)

    assert report.details[0].status == "source_absent_after_prior_inventory"
    assert summary["candidate_coverage"]["matched"] == 0
    assert summary["certified_coverage"] == {
        "status": "not_implemented",
        "certified": 0,
    }
    assert summary["ready_for_next_writer"] is True


@pytest.mark.parametrize(
    "current",
    [
        (_native("host-a", _source("codex-cli", "native-copy", source_copies=2)),),
        (
            _native("host-a", _source("codex-cli", "native-copy")),
            _native("host-b", _source("codex-cli", "native-copy")),
        ),
    ],
    ids=["multiple-files-within-host", "same-source-across-hosts"],
)
def test_repeated_current_source_identities_are_duplicate_groups(current):
    pond = _pond(_archive("native-copy", "codex-cli"))

    summary = coverage_summary(build_coverage_report((), current, pond))

    assert summary["collisions"]["duplicate_source_groups"] == 1
    assert summary["current_source_coverage"]["matched"] == len(current)
    assert summary["ready_for_next_writer"] is False


@pytest.mark.parametrize(
    ("registry", "current", "prior", "pond"),
    [
        (
            (
                _candidate("drover-a", "host-a", "codex", "native-collision"),
                _candidate("drover-b", "host-b", "claude-code", "native-collision"),
            ),
            (),
            (),
            _pond(),
        ),
        (
            (),
            (
                _native(
                    "host-a",
                    _source("codex-cli", "native-collision"),
                    _source("claude-code", "native-collision"),
                ),
            ),
            (),
            _pond(
                _archive("native-collision", "codex-cli"),
                _archive(
                    "native-collision",
                    "claude-code",
                    created_at="2026-08-28T11:00:00Z",
                ),
            ),
        ),
        (
            (),
            (),
            (
                _native("host-a", _source("codex-cli", "native-collision")),
                _native("host-a", _source("claude-code", "native-collision")),
            ),
            _pond(),
        ),
        (
            (),
            (),
            (),
            _pond(
                _archive("native-collision", "codex-cli"),
                _archive(
                    "native-collision",
                    "claude-code",
                    created_at="2026-08-28T11:00:00Z",
                ),
            ),
        ),
    ],
    ids=["registry", "current-source", "prior-source", "pond"],
)
def test_cross_harness_native_id_collisions_from_every_inventory_block_readiness(
    registry, current, prior, pond
):
    summary = coverage_summary(
        build_coverage_report(registry, current, pond, prior_sources=prior)
    )

    assert summary["collisions"]["cross_harness_native_id_groups"] == 1
    assert summary["ready_for_next_writer"] is False


def test_distinct_pond_sessions_with_one_nonempty_signature_block_readiness():
    pond = _pond(
        _archive("pond-a", "codex-cli"),
        _archive("pond-b", "codex-cli"),
    )

    report = build_coverage_report((), (), pond)
    summary = coverage_summary(report)

    assert summary["collisions"]["archive_logical_duplicate_candidate_groups"] == 1
    assert summary["collisions"]["archive_signature_unverifiable"] == 0
    assert summary["ready_for_next_writer"] is False
    wire = report.to_wire()
    assert wire["collisions"]["archive_logical_duplicate_candidate_groups"][0][
        "pond_session_ids"
    ] == ["pond-a", "pond-b"]


def test_zero_message_pond_sessions_are_unverifiable_and_not_duplicate_candidates():
    pond = _pond(
        _archive(
            "pond-empty-a",
            "codex-cli",
            message_count=0,
            first_message_at=None,
            last_message_at=None,
        ),
        _archive(
            "pond-empty-b",
            "codex-cli",
            message_count=0,
            first_message_at=None,
            last_message_at=None,
        ),
    )

    summary = coverage_summary(build_coverage_report((), (), pond))

    assert summary["collisions"]["archive_logical_duplicate_candidate_groups"] == 0
    assert summary["collisions"]["archive_signature_unverifiable"] == 2
    assert summary["ready_for_next_writer"] is False


def test_historical_candidate_misses_alone_do_not_block_readiness():
    registry = (
        _candidate("drover-prior", "host-a", "codex", "native-prior"),
        _candidate("drover-unknown", "host-b", "claude-code", "native-unknown"),
    )
    prior = (_native("host-a", _source("codex-cli", "native-prior")),)

    summary = coverage_summary(
        build_coverage_report(registry, (), _pond(), prior_sources=prior)
    )

    assert summary["misses"] == {
        "discovered_not_synced": 0,
        "source_absent_after_prior_inventory": 1,
        "unverifiable": 1,
    }
    assert summary["ready_for_next_writer"] is True


def test_coverage_refuses_an_inventory_from_an_unpinned_pond_version():
    pond = PondInventory(1, _CAPTURED_AT, "0.16.2", ())

    with pytest.raises(ValueError, match="pond inventory"):
        build_coverage_report((), (), pond)


@pytest.mark.parametrize("message_count", [0, 1], ids=["empty", "nonempty"])
def test_coverage_rejects_unsupported_pond_agents_without_exposing_identifiers(
    message_count,
):
    unsupported_agent = "private-unsupported-pond-agent"
    private_session_id = "private-unsupported-pond-session"
    pond = _pond(
        _archive(
            private_session_id,
            unsupported_agent,
            message_count=message_count,
            first_message_at=(None if message_count == 0 else "2026-08-28T10:01:00Z"),
            last_message_at=(None if message_count == 0 else "2026-08-28T10:02:00Z"),
        )
    )

    with pytest.raises(
        ValueError, match=r"^archive coverage pond inventory$"
    ) as raised:
        build_coverage_report((), (), pond)

    assert unsupported_agent not in str(raised.value)
    assert private_session_id not in str(raised.value)


def test_private_report_keeps_investigation_ids_while_public_summary_is_recursive_safe():
    sensitive_values = (
        "drover-id-private",
        "private-hostname",
        "/Users/operator/private-path",
        "project-secret",
        "pond-id-private",
        "do-not-copy-this-transcript",
    )
    drover_id = f"{sensitive_values[0]}-{sensitive_values[5]}"
    host_id = f"{sensitive_values[1]}-{sensitive_values[3]}"
    native_id = f"native-id-private-{sensitive_values[2]}"
    registry = (
        _candidate(
            drover_id,
            host_id,
            "codex",
            native_id,
        ),
    )
    pond = _pond(
        _archive(
            native_id,
            "codex-cli",
            created_at="2026-08-28T09:00:00Z",
            first_message_at="2026-08-28T09:01:00Z",
            last_message_at="2026-08-28T09:02:00Z",
        ),
        _archive(
            sensitive_values[4],
            "claude-code",
            created_at="2026-08-28T08:00:00Z",
            message_count=0,
            first_message_at=None,
            last_message_at=None,
        ),
    )

    report = build_coverage_report(registry, (), pond)
    private_serialized = json.dumps(report.to_wire(), sort_keys=True)
    public_serialized = json.dumps(coverage_summary(report), sort_keys=True)

    for value in sensitive_values:
        assert value in private_serialized
    for value in sensitive_values:
        assert value not in public_serialized
    assert set(coverage_summary(report)["candidate_coverage"]["by_harness"]) <= {
        "claude-code",
        "codex-cli",
    }
