"""Pure candidate-coverage classification for private archive inventories."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

import duckdb

from drover.server.archive.inventory import (
    NativeInventory,
    PondInventory,
    SourceEligibilityReceipt,
)
from drover.server.archive.pond_inventory import (
    POND_VERSION,
    _pond_source_agent_family,
)

_ROOT_SOURCE_AGENTS = frozenset({"claude-code", "codex-cli"})
_REGISTRY_HARNESS_MAP = {
    "claude-code": "claude-code",
    "codex": "codex-cli",
    "codex-cli": "codex-cli",
}
_CoverageStatus = Literal[
    "matched",
    "discovered_not_synced",
    "source_absent_after_prior_inventory",
    "source_not_archive_eligible",
    "unverifiable",
]


def _failure(category: str) -> ValueError:
    return ValueError(f"archive coverage {category}")


@dataclass(frozen=True, slots=True)
class RegistryCandidate:
    """One native-identity-bearing Drover registry row."""

    session_id: str
    host_id: str
    harness: str
    native_session_id: str


@dataclass(frozen=True, slots=True)
class CoverageDetail:
    """Private classification of one eligible Drover registry row."""

    drover_session_id: str
    host_id: str
    source_agent: str
    native_session_id: str
    pond_session_id: str | None
    status: _CoverageStatus

    def to_wire(self) -> dict[str, object]:
        return {
            "drover_session_id": self.drover_session_id,
            "host_id": self.host_id,
            "source_agent": self.source_agent,
            "native_session_id": self.native_session_id,
            "pond_session_id": self.pond_session_id,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class _CurrentSourceDetail:
    host_id: str
    source_agent: str
    native_session_id: str
    pond_session_id: str | None
    status: Literal["matched", "discovered_not_synced", "source_not_archive_eligible"]

    def to_wire(self) -> dict[str, object]:
        return {
            "host_id": self.host_id,
            "source_agent": self.source_agent,
            "native_session_id": self.native_session_id,
            "pond_session_id": self.pond_session_id,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class _SourceLocation:
    host_id: str
    source_copies: int

    def to_wire(self) -> dict[str, object]:
        return {"host_id": self.host_id, "source_copies": self.source_copies}


@dataclass(frozen=True, slots=True)
class _DuplicateSourceGroup:
    source_agent: str
    native_session_id: str
    locations: tuple[_SourceLocation, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "source_agent": self.source_agent,
            "native_session_id": self.native_session_id,
            "locations": [location.to_wire() for location in self.locations],
        }


@dataclass(frozen=True, slots=True)
class _CrossHarnessNativeIdGroup:
    native_session_id: str
    source_agents: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "native_session_id": self.native_session_id,
            "source_agents": list(self.source_agents),
        }


@dataclass(frozen=True, slots=True)
class _ArchiveLogicalDuplicateGroup:
    source_agent: str
    created_at: str
    message_count: int
    first_message_at: str
    last_message_at: str
    pond_session_ids: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "source_agent": self.source_agent,
            "created_at": self.created_at,
            "message_count": self.message_count,
            "first_message_at": self.first_message_at,
            "last_message_at": self.last_message_at,
            "pond_session_ids": list(self.pond_session_ids),
        }


@dataclass(frozen=True, slots=True)
class _ArchiveSignatureUnverifiable:
    source_agent: str
    pond_session_id: str

    def to_wire(self) -> dict[str, object]:
        return {
            "source_agent": self.source_agent,
            "pond_session_id": self.pond_session_id,
        }


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Private coverage evidence plus the conservative writer-readiness result."""

    schema_version: int
    details: tuple[CoverageDetail, ...]
    current_source_details: tuple[_CurrentSourceDetail, ...]
    duplicate_source_groups: tuple[_DuplicateSourceGroup, ...]
    cross_harness_native_id_groups: tuple[_CrossHarnessNativeIdGroup, ...]
    archive_logical_duplicate_candidate_groups: tuple[
        _ArchiveLogicalDuplicateGroup, ...
    ]
    archive_signature_unverifiable: tuple[_ArchiveSignatureUnverifiable, ...]
    unsupported_harness_sessions: int
    ready_for_next_writer: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": "archive_coverage_report",
            "schema_version": self.schema_version,
            "details": [detail.to_wire() for detail in self.details],
            "current_source_details": [
                detail.to_wire() for detail in self.current_source_details
            ],
            "certified_coverage": {
                "status": "not_implemented",
                "certified": 0,
            },
            "collisions": {
                "duplicate_source_groups": [
                    group.to_wire() for group in self.duplicate_source_groups
                ],
                "cross_harness_native_id_groups": [
                    group.to_wire() for group in self.cross_harness_native_id_groups
                ],
                "archive_logical_duplicate_candidate_groups": [
                    group.to_wire()
                    for group in self.archive_logical_duplicate_candidate_groups
                ],
                "archive_signature_unverifiable": [
                    detail.to_wire() for detail in self.archive_signature_unverifiable
                ],
            },
            "unsupported_harness_sessions": self.unsupported_harness_sessions,
            "ready_for_next_writer": self.ready_for_next_writer,
        }


def load_registry_candidates(
    control_plane_db: Path,
) -> tuple[RegistryCandidate, ...]:
    """Project eligible rows from an already-copied registry without writing it."""
    try:
        with duckdb.connect(str(control_plane_db), read_only=True) as connection:
            rows = connection.execute("""
                SELECT session_id, host_id, harness, native_session_id
                FROM harness_sessions
                WHERE native_session_id IS NOT NULL
                  AND trim(native_session_id) <> ''
                ORDER BY host_id, harness, session_id
                """).fetchall()
    except Exception:
        raise _failure("registry") from None

    candidates: list[RegistryCandidate] = []
    for row in rows:
        if len(row) != 4 or not all(isinstance(value, str) for value in row):
            raise _failure("registry")
        candidates.append(RegistryCandidate(*row))
    return tuple(candidates)


def _validated_native_sources(
    inventories: Sequence[NativeInventory],
    *,
    current: bool,
) -> tuple[NativeInventory, ...]:
    result = tuple(inventories)
    seen_hosts: set[str] = set()
    for inventory in result:
        try:
            inventory.to_wire()
        except (AttributeError, TypeError, ValueError):
            raise _failure("source inventory") from None
        if current and inventory.host_id in seen_hosts:
            raise _failure("current source inventory")
        seen_hosts.add(inventory.host_id)
        if any(
            record.source_agent not in _ROOT_SOURCE_AGENTS
            for record in inventory.records
        ):
            raise _failure("source inventory")
    return result


def _validated_pond(pond: PondInventory) -> PondInventory:
    try:
        pond.to_wire()
    except (AttributeError, TypeError, ValueError):
        raise _failure("pond inventory") from None
    if pond.pond_version != POND_VERSION:
        raise _failure("pond inventory")
    if any(
        _pond_source_agent_family(record.source_agent) is None
        for record in pond.records
    ):
        raise _failure("pond inventory")
    return pond


def _eligible_registry(
    registry: Sequence[RegistryCandidate],
) -> tuple[tuple[RegistryCandidate, str], ...]:
    eligible: list[tuple[RegistryCandidate, str]] = []
    for candidate in registry:
        if type(candidate) is not RegistryCandidate:
            raise _failure("registry")
        values = (
            candidate.session_id,
            candidate.host_id,
            candidate.harness,
            candidate.native_session_id,
        )
        if not all(isinstance(value, str) for value in values):
            raise _failure("registry")
        if not candidate.native_session_id.strip():
            continue
        source_agent = _REGISTRY_HARNESS_MAP.get(candidate.harness)
        if source_agent is not None:
            eligible.append((candidate, source_agent))
    return tuple(
        sorted(
            eligible,
            key=lambda item: (
                item[0].host_id,
                item[0].harness,
                item[0].session_id,
            ),
        )
    )


def _validated_receipts(
    receipts: Sequence[SourceEligibilityReceipt],
    current: Sequence[NativeInventory],
) -> dict[tuple[str, str, str], SourceEligibilityReceipt]:
    current_records = {
        (inventory.host_id, record.source_agent, record.session_id): record
        for inventory in current
        for record in inventory.records
    }
    source_identity_counts = Counter(
        (record.source_agent, record.session_id)
        for inventory in current
        for record in inventory.records
    )
    validated: dict[tuple[str, str, str], SourceEligibilityReceipt] = {}
    for receipt in receipts:
        try:
            receipt.to_wire()
        except (AttributeError, TypeError, ValueError):
            raise _failure("eligibility receipt") from None
        key = (receipt.host_id, receipt.source_agent, receipt.session_id)
        record = current_records.get(key)
        if (
            key in validated
            or record is None
            or record.source_copies != 1
            or source_identity_counts[(record.source_agent, record.session_id)] != 1
            or record.source_fingerprint != receipt.source_fingerprint
        ):
            raise _failure("eligibility receipt")
        validated[key] = receipt
    return validated


def build_coverage_report(
    registry: Sequence[RegistryCandidate],
    current_sources: Sequence[NativeInventory],
    pond: PondInventory,
    *,
    prior_sources: Sequence[NativeInventory] = (),
    eligibility_receipts: Sequence[SourceEligibilityReceipt] = (),
) -> CoverageReport:
    """Join private inventories without mutating the registry or archive."""
    registry_rows = tuple(registry)
    current = _validated_native_sources(current_sources, current=True)
    prior = _validated_native_sources(prior_sources, current=False)
    archive = _validated_pond(pond)
    receipts_by_identity = _validated_receipts(eligibility_receipts, current)
    eligible = _eligible_registry(registry_rows)
    unsupported_harness_sessions = sum(
        1
        for candidate in registry_rows
        if candidate.native_session_id.strip()
        and candidate.harness not in _REGISTRY_HARNESS_MAP
    )

    pond_by_identity = {}
    for record in archive.records:
        source_agent_family = _pond_source_agent_family(record.source_agent)
        assert source_agent_family is not None
        identity = (source_agent_family, record.session_id)
        if identity in pond_by_identity:
            raise _failure("pond inventory")
        pond_by_identity[identity] = record
    current_by_host_identity = {
        (inventory.host_id, record.source_agent, record.session_id)
        for inventory in current
        for record in inventory.records
    }
    prior_by_host_identity = {
        (inventory.host_id, record.source_agent, record.session_id)
        for inventory in prior
        for record in inventory.records
    }

    details: list[CoverageDetail] = []
    for candidate, source_agent in eligible:
        pond_record = pond_by_identity.get((source_agent, candidate.native_session_id))
        host_identity = (
            candidate.host_id,
            source_agent,
            candidate.native_session_id,
        )
        if pond_record is not None:
            status: _CoverageStatus = "matched"
        elif host_identity in receipts_by_identity:
            status = "source_not_archive_eligible"
        elif host_identity in current_by_host_identity:
            status = "discovered_not_synced"
        elif host_identity in prior_by_host_identity:
            status = "source_absent_after_prior_inventory"
        else:
            status = "unverifiable"
        details.append(
            CoverageDetail(
                drover_session_id=candidate.session_id,
                host_id=candidate.host_id,
                source_agent=source_agent,
                native_session_id=candidate.native_session_id,
                pond_session_id=(
                    pond_record.session_id if pond_record is not None else None
                ),
                status=status,
            )
        )

    current_source_details: list[_CurrentSourceDetail] = []
    source_locations: defaultdict[tuple[str, str], list[_SourceLocation]] = defaultdict(
        list
    )
    for inventory in sorted(current, key=lambda value: value.host_id):
        for record in sorted(
            inventory.records,
            key=lambda value: (value.source_agent, value.session_id),
        ):
            pond_record = pond_by_identity.get((record.source_agent, record.session_id))
            source_identity = (
                inventory.host_id,
                record.source_agent,
                record.session_id,
            )
            receipt = receipts_by_identity.get(source_identity)
            if pond_record is not None and receipt is not None:
                raise _failure("eligibility receipt")
            current_source_details.append(
                _CurrentSourceDetail(
                    host_id=inventory.host_id,
                    source_agent=record.source_agent,
                    native_session_id=record.session_id,
                    pond_session_id=(
                        pond_record.session_id if pond_record is not None else None
                    ),
                    status=(
                        "matched"
                        if pond_record is not None
                        else (
                            "source_not_archive_eligible"
                            if receipt is not None
                            else "discovered_not_synced"
                        )
                    ),
                )
            )
            source_locations[(record.source_agent, record.session_id)].append(
                _SourceLocation(inventory.host_id, record.source_copies)
            )

    duplicate_source_groups = tuple(
        _DuplicateSourceGroup(source_agent, native_session_id, tuple(locations))
        for (source_agent, native_session_id), locations in sorted(
            source_locations.items()
        )
        if len(locations) > 1
        or any(location.source_copies > 1 for location in locations)
    )

    agents_by_native_id: defaultdict[str, set[str]] = defaultdict(set)
    for candidate, source_agent in eligible:
        agents_by_native_id[candidate.native_session_id].add(source_agent)
    for inventory in (*current, *prior):
        for record in inventory.records:
            agents_by_native_id[record.session_id].add(record.source_agent)
    for record in archive.records:
        source_agent_family = _pond_source_agent_family(record.source_agent)
        assert source_agent_family is not None
        agents_by_native_id[record.session_id].add(source_agent_family)
    cross_harness_native_id_groups = tuple(
        _CrossHarnessNativeIdGroup(native_session_id, tuple(sorted(source_agents)))
        for native_session_id, source_agents in sorted(agents_by_native_id.items())
        if len(source_agents) > 1
    )

    sessions_by_signature: defaultdict[tuple[str, str, int, str, str], list[str]] = (
        defaultdict(list)
    )
    signature_unverifiable: list[_ArchiveSignatureUnverifiable] = []
    for record in archive.records:
        if record.message_count == 0:
            signature_unverifiable.append(
                _ArchiveSignatureUnverifiable(record.source_agent, record.session_id)
            )
            continue
        assert record.first_message_at is not None
        assert record.last_message_at is not None
        signature = (
            record.source_agent,
            record.created_at,
            record.message_count,
            record.first_message_at,
            record.last_message_at,
        )
        sessions_by_signature[signature].append(record.session_id)
    archive_logical_duplicate_candidate_groups = tuple(
        _ArchiveLogicalDuplicateGroup(*signature, tuple(sorted(session_ids)))
        for signature, session_ids in sorted(sessions_by_signature.items())
        if len(set(session_ids)) > 1
    )
    archive_signature_unverifiable = tuple(
        sorted(
            signature_unverifiable,
            key=lambda detail: (detail.source_agent, detail.pond_session_id),
        )
    )

    current_not_synced = any(
        detail.status == "discovered_not_synced" for detail in current_source_details
    )
    ready_for_next_writer = not (
        current_not_synced
        or duplicate_source_groups
        or cross_harness_native_id_groups
        or archive_logical_duplicate_candidate_groups
        or archive_signature_unverifiable
    )
    return CoverageReport(
        schema_version=1,
        details=tuple(details),
        current_source_details=tuple(current_source_details),
        duplicate_source_groups=duplicate_source_groups,
        cross_harness_native_id_groups=cross_harness_native_id_groups,
        archive_logical_duplicate_candidate_groups=(
            archive_logical_duplicate_candidate_groups
        ),
        archive_signature_unverifiable=archive_signature_unverifiable,
        unsupported_harness_sessions=unsupported_harness_sessions,
        ready_for_next_writer=ready_for_next_writer,
    )


def coverage_summary(report: CoverageReport) -> dict[str, object]:
    """Return the exact aggregate-only coverage contract for public output."""
    if type(report) is not CoverageReport or report.schema_version != 1:
        raise _failure("summary")
    by_harness: dict[str, dict[str, int]] = {}
    for source_agent in sorted(_ROOT_SOURCE_AGENTS):
        harness_details = [
            detail for detail in report.details if detail.source_agent == source_agent
        ]
        if harness_details:
            by_harness[source_agent] = {
                "eligible": len(harness_details),
                "matched": sum(
                    detail.status == "matched" for detail in harness_details
                ),
            }
    eligible = len(report.details)
    matched = sum(detail.status == "matched" for detail in report.details)
    current_matched = sum(
        detail.status == "matched" for detail in report.current_source_details
    )
    current_not_archive_eligible = sum(
        detail.status == "source_not_archive_eligible"
        for detail in report.current_source_details
    )
    current_discovered = len(report.current_source_details)
    return {
        "schema_version": 1,
        "candidate_coverage": {
            "eligible": eligible,
            "matched": matched,
            "percent": round(matched * 100 / eligible, 1) if eligible else 0.0,
            "by_harness": by_harness,
        },
        "current_source_coverage": {
            "discovered": current_discovered,
            "matched": current_matched,
            "source_not_archive_eligible": current_not_archive_eligible,
            "discovered_not_synced": (
                current_discovered - current_matched - current_not_archive_eligible
            ),
        },
        "certified_coverage": {"status": "not_implemented", "certified": 0},
        "misses": {
            "discovered_not_synced": sum(
                detail.status == "discovered_not_synced" for detail in report.details
            ),
            "source_absent_after_prior_inventory": sum(
                detail.status == "source_absent_after_prior_inventory"
                for detail in report.details
            ),
            "unverifiable": sum(
                detail.status == "unverifiable" for detail in report.details
            ),
        },
        "collisions": {
            "duplicate_source_groups": len(report.duplicate_source_groups),
            "cross_harness_native_id_groups": len(
                report.cross_harness_native_id_groups
            ),
            "archive_logical_duplicate_candidate_groups": len(
                report.archive_logical_duplicate_candidate_groups
            ),
            "archive_signature_unverifiable": len(
                report.archive_signature_unverifiable
            ),
        },
        "unsupported_harness_sessions": report.unsupported_harness_sessions,
        "ready_for_next_writer": report.ready_for_next_writer,
    }
