"""The one-way removal of rows from the retired legacy importer."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_control_outbox import postgres_control_store
from test_lake_exporter import seed
from test_lake_runtime import lake_spec
from test_lake_serving import verified_lake


def _legacy_fixture(verified_lake, postgres_control_store):
    from drover.server.lake.exporter import LakeOutboxExporter, provision_exporter
    from drover.server.lake.rebuild_worker import POLICY_SCHEMA
    from drover.server.lake.runtime import lake_connection

    spec, control_path, _ = verified_lake
    provision_exporter(spec)
    seed(control_path, count=1)
    with LakeOutboxExporter(control_path=control_path, spec=spec) as exporter:
        assert exporter.run_once()["acknowledged"] == 1
    expression = (
        "sha256(to_json(struct_pack("
        + ", ".join(f'"{name}" := "{name}"' for name in POLICY_SCHEMA)
        + ")))"
    )
    with lake_connection(spec, read_only=False) as con:
        # Model the crossover day: one real exporter row happened to retain an
        # old label, while a distinct legacy row has no version evidence.
        event_id, day = con.execute(
            "SELECT id,date FROM lake.agent_events WHERE _import_file LIKE 'outbox:%'"
        ).fetchone()
        con.execute(
            "UPDATE lake.agent_events SET _import_file='legacy-import:mixed' WHERE id=?",
            [event_id],
        )
        con.execute(
            "INSERT INTO lake.agent_events BY NAME SELECT * REPLACE ("
            + expression
            + " AS _row_sha256) FROM (SELECT * REPLACE "
            "('legacy-unreceipted' AS id, 'legacy-import:mixed' AS _import_file) "
            "FROM lake.agent_events WHERE id=?)",
            [event_id],
        )
    return spec, day, event_id


def test_deprecation_dry_run_is_exact_and_does_not_write(
    verified_lake, postgres_control_store
):
    from drover.server.lake.legacy_deprecation import deprecate_legacy
    from drover.server.lake.runtime import lake_connection

    spec, day, _ = _legacy_fixture(verified_lake, postgres_control_store)
    with lake_connection(spec) as con:
        before_snapshot = con.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchone()[0]
        before_rows = con.execute("SELECT count(*) FROM lake.agent_events").fetchone()[
            0
        ]
    report = deprecate_legacy(spec)
    assert report["dry_run"] is True
    assert report["tables"]["agent_events"] == [{"day": day, "rows": 1}]
    with lake_connection(spec) as con:
        assert (
            con.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]
            == before_snapshot
        )
        assert (
            con.execute("SELECT count(*) FROM lake.agent_events").fetchone()[0]
            == before_rows
        )


def test_deprecation_keeps_receipted_crossover_rows_and_recertifies(
    verified_lake, postgres_control_store
):
    from drover.server.lake.legacy_deprecation import deprecate_legacy
    from drover.server.lake.rebuild import verify
    from drover.server.lake.runtime import lake_connection
    from drover.server.lake.serving_proof import check_proof

    spec, _, event_id = _legacy_fixture(verified_lake, postgres_control_store)
    report = deprecate_legacy(spec, apply=True)
    assert report["no_op"] is False
    assert report["tables"]["agent_events"]
    with lake_connection(spec) as con:
        assert con.execute(
            "SELECT count(*) FROM lake.agent_events WHERE id='legacy-unreceipted'"
        ).fetchone() == (0,)
        # Same legacy label, but this exact row is an exporter version.
        assert con.execute(
            "SELECT count(*) FROM lake.agent_events WHERE id=? AND _import_file LIKE 'legacy-import:%'",
            [event_id],
        ).fetchone() == (1,)
        assert (
            check_proof(spec, con, report["proof_sha256"])["snapshot"]
            == report["serving_snapshot"]
        )
    assert verify(spec)["agent_events"]["rows"] >= 1

    repeat = deprecate_legacy(spec, apply=True)
    assert repeat["no_op"] is True


def test_deprecation_fails_safely_behind_an_existing_mutation_fence(
    verified_lake, postgres_control_store
):
    from drover.server.lake.fence import MutationFence
    from drover.server.lake.legacy_deprecation import deprecate_legacy
    from drover.server.lake.runtime import LakeError

    spec, _, _ = _legacy_fixture(verified_lake, postgres_control_store)
    with MutationFence(spec.dsn()):
        with pytest.raises(LakeError, match="mutation_fenced"):
            deprecate_legacy(spec, apply=True)


def test_deprecation_does_not_make_nonlegacy_unreceipted_rows_valid(
    verified_lake, postgres_control_store
):
    from drover.server.lake.legacy_deprecation import deprecate_legacy
    from drover.server.lake.rebuild import verify
    from drover.server.lake.rebuild_worker import POLICY_SCHEMA
    from drover.server.lake.runtime import LakeError, lake_connection

    spec, _, event_id = _legacy_fixture(verified_lake, postgres_control_store)
    deprecate_legacy(spec, apply=True)
    expression = (
        "sha256(to_json(struct_pack("
        + ", ".join(f'"{name}" := "{name}"' for name in POLICY_SCHEMA)
        + ")))"
    )
    with lake_connection(spec, read_only=False) as con:
        con.execute(
            "INSERT INTO lake.agent_events BY NAME SELECT * REPLACE ("
            + expression
            + " AS _row_sha256) FROM (SELECT * REPLACE "
            "('ordinary-unreceipted' AS id, 'manual:test' AS _import_file) "
            "FROM lake.agent_events WHERE id=?)",
            [event_id],
        )
    with pytest.raises(LakeError, match="unexpected_partition"):
        verify(spec)


def test_retired_importer_refuses_before_it_can_write():
    from drover.server.lake.legacy_import import run_import
    from drover.server.lake.runtime import LakeError

    with pytest.raises(LakeError, match="legacy_import_deprecated"):
        run_import(None, Path("/not-used"), since="2026-10-01")
