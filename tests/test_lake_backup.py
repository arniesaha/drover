import json

import pytest

from drover.server.lake import backup
from drover.server.lake.backup import STAGING_MARKER, create_backup, restore_backup
from drover.server.lake.runtime import LakeError


@pytest.fixture(autouse=True)
def parquet_metadata(monkeypatch):
    monkeypatch.setattr(
        backup, "_parquet_rows", lambda path: 2 if path.name == "events.parquet" else 1
    )


def staging_copy(tmp_path):
    root = tmp_path / "studio-copy"
    root.joinpath("partitions", "2026-10-01").mkdir(parents=True)
    root.joinpath("partitions", "provider_usage_snapshots", "0").mkdir(parents=True)
    root.joinpath("partitions", "control_outbox_batches", "0").mkdir(parents=True)
    root.joinpath("verification").mkdir()
    root.joinpath(STAGING_MARKER).write_text("DROVER_ISOLATED_STAGING_COPY\n")
    root.joinpath("partitions/2026-10-01/events.parquet").write_bytes(
        b"staging parquet events"
    )
    root.joinpath("partitions/2026-10-01/metadata.parquet").write_bytes(
        b"staging parquet metadata"
    )
    root.joinpath("partitions/provider_usage_snapshots/0/rows.parquet").write_bytes(
        b"usage"
    )
    root.joinpath("partitions/control_outbox_batches/0/rows.parquet").write_bytes(
        b"outbox"
    )
    root.joinpath("verification", "report.json").write_text(
        json.dumps(
            {
                "canonical_agent_events": {"rows": 2},
                "legacy_metadata": {"rows": 1},
                "raw": {
                    "provider_usage_snapshots": {"rows": 1},
                    "control_outbox_batches": {"rows": 1},
                },
            }
        )
    )
    return root


def test_backup_and_restore_are_immutable_and_verify_rows(tmp_path):
    source = staging_copy(tmp_path)
    catalog = tmp_path / "studio-catalog.snapshot"
    catalog.write_bytes(b"isolated pg_dump copy")
    receipt = create_backup(
        source, catalog, tmp_path / "backups", generation_id="rehearsal-1"
    )
    generation = tmp_path / "backups/rehearsal-1"
    assert receipt["verified"] and receipt["row_counts"] == {
        "agent_events": 2,
        "agent_events_legacy_metadata": 1,
        "provider_usage_snapshots": 1,
        "control_outbox_batches": 1,
    }
    manifest = json.loads(generation.joinpath("manifest.json").read_text())
    assert {entry["path"] for entry in manifest["files"]} == {
        "catalog/catalog.snapshot",
        "data/partitions/2026-10-01/events.parquet",
        "data/partitions/2026-10-01/metadata.parquet",
        "data/partitions/provider_usage_snapshots/0/rows.parquet",
        "data/partitions/control_outbox_batches/0/rows.parquet",
        "data/verification/report.json",
    }
    restored = restore_backup(
        generation, tmp_path / "restored-data", tmp_path / "restored/catalog.snapshot"
    )
    assert restored == {
        "generation_id": "rehearsal-1",
        "row_counts": {
            "agent_events": 2,
            "agent_events_legacy_metadata": 1,
            "provider_usage_snapshots": 1,
            "control_outbox_batches": 1,
        },
        "verified": True,
    }
    assert (tmp_path / "restored-data/partitions/2026-10-01/events.parquet").is_file()
    assert (tmp_path / "restored/catalog.snapshot").read_bytes() == catalog.read_bytes()


def test_backup_fails_closed_without_marked_staging_copy(tmp_path):
    source = staging_copy(tmp_path)
    source.joinpath(STAGING_MARKER).unlink()
    catalog = tmp_path / "catalog.snapshot"
    catalog.write_bytes(b"copy")
    with pytest.raises(LakeError, match="lake_backup_isolated_staging_marker_required"):
        create_backup(source, catalog, tmp_path / "backups")
    assert not (tmp_path / "backups").exists()


def test_restore_rejects_tampered_artifact_before_writing(tmp_path):
    source = staging_copy(tmp_path)
    catalog = tmp_path / "catalog.snapshot"
    catalog.write_bytes(b"copy")
    create_backup(source, catalog, tmp_path / "backups", generation_id="rehearsal-2")
    artifact = (
        tmp_path / "backups/rehearsal-2/data/partitions/2026-10-01/events.parquet"
    )
    artifact.write_bytes(b"tampered")
    with pytest.raises(LakeError, match="lake_restore_artifact_verification_failed"):
        restore_backup(
            tmp_path / "backups/rehearsal-2", tmp_path / "out", tmp_path / "catalog-out"
        )
    assert not (tmp_path / "out").exists()


def test_backup_rejects_existing_generation(tmp_path):
    source = staging_copy(tmp_path)
    catalog = tmp_path / "catalog.snapshot"
    catalog.write_bytes(b"copy")
    create_backup(source, catalog, tmp_path / "backups", generation_id="rehearsal-3")
    with pytest.raises(LakeError, match="lake_backup_generation_exists"):
        create_backup(
            source, catalog, tmp_path / "backups", generation_id="rehearsal-3"
        )
