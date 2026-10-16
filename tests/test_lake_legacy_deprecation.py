"""The one-way removal of rows from the retired legacy importer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
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
        con.execute("CREATE TABLE lake.import_watermark(partition_date VARCHAR)")
        con.execute("INSERT INTO lake.import_watermark VALUES ('2026-01-01')")
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
    from drover.server.lake.serving import check_selected, configure_analytics
    from drover.server.lake.serving_proof import check_proof

    spec, path, config = verified_lake
    spec, _, event_id = _legacy_fixture(verified_lake, postgres_control_store)
    report = deprecate_legacy(spec, apply=True)
    assert report["no_op"] is False
    assert report["tables"]["agent_events"]
    digest = hashlib.sha256(
        (spec.data_root / "verification/serving-proof.json").read_bytes()
    ).hexdigest()
    assert report["proof_sha256"] == digest
    configure_analytics(path, replace(config, verification_sha256=digest))
    check_selected(path)
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


def test_deprecation_cli_prints_published_proof_pin(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from drover.server.lake import cli, legacy_deprecation

    proof = tmp_path / "verification" / "serving-proof.json"
    proof.parent.mkdir()
    proof.write_bytes(b"published-proof")
    digest = hashlib.sha256(proof.read_bytes()).hexdigest()
    monkeypatch.setenv("DROVER_LAKE_EXTENSION_DIR", str(tmp_path))
    monkeypatch.setenv("DROVER_LAKE_ENGINE_SHA256", "b" * 64)
    monkeypatch.setattr(
        legacy_deprecation,
        "deprecate_legacy",
        lambda spec, apply: {"dry_run": False, "no_op": False, "proof_sha256": digest},
    )

    result = CliRunner().invoke(
        cli.lake_cmd,
        [
            "deprecate-legacy",
            "--data-root",
            str(tmp_path),
            "--catalog-dsn-env",
            "TEST_DSN",
            "--apply",
        ],
    )

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    receipt = json.loads("\n".join(lines[:-1]))
    hint = f'[analytics] verification_sha256 = "{digest}"'
    assert receipt["verification_sha256"] == digest
    assert receipt["verification_sha256_hint"] == hint
    assert lines[-1] == hint


def test_rebuild_cli_does_not_print_a_pin_before_verification(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from drover.server.lake import cli

    source = tmp_path / "source.tar"
    source.write_bytes(b"not-used-by-mocked-rebuild")
    monkeypatch.setenv("DROVER_LAKE_EXTENSION_DIR", str(tmp_path))
    monkeypatch.setenv("DROVER_LAKE_ENGINE_SHA256", "b" * 64)
    monkeypatch.setattr(cli, "rebuild", lambda *args, **kwargs: {"dry_run": False})

    result = CliRunner().invoke(
        cli.lake_cmd,
        [
            "rebuild",
            "--from-tar",
            str(source),
            "--data-root",
            str(tmp_path),
            "--catalog-dsn-env",
            "TEST_DSN",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"dry_run": False}


def test_deprecation_removes_legacy_hash_duplicates_beyond_receipt_count(
    verified_lake, postgres_control_store
):
    from drover.server.lake.legacy_deprecation import deprecate_legacy
    from drover.server.lake.runtime import lake_connection

    spec, day, event_id = _legacy_fixture(verified_lake, postgres_control_store)
    with lake_connection(spec, read_only=False) as con:
        con.execute(
            "UPDATE lake.agent_events SET _import_file='outbox:preserved' WHERE id=?",
            [event_id],
        )
        # This copy has the exact exporter-version hash.  Provenance alone is
        # insufficient: the non-legacy exporter copy must survive and this
        # legacy duplicate must not consume its receipt capacity.
        con.execute(
            "INSERT INTO lake.agent_events BY NAME SELECT * REPLACE "
            "('legacy-import:duplicate' AS _import_file) "
            "FROM lake.agent_events WHERE id=?",
            [event_id],
        )
    report = deprecate_legacy(spec, apply=True)
    assert report["tables"]["agent_events"] == [{"day": day, "rows": 2}]
    with lake_connection(spec) as con:
        assert con.execute(
            "SELECT count(*) FROM lake.agent_events "
            "WHERE date=? AND _row_sha256 IN "
            "(SELECT _row_sha256 FROM lake.export_event_versions WHERE id=?)",
            [day, event_id],
        ).fetchone() == (1,)


def test_deprecation_rolls_back_when_remaining_rows_would_fail_verify(
    verified_lake, postgres_control_store
):
    from drover.server.lake.legacy_deprecation import deprecate_legacy
    from drover.server.lake.rebuild_worker import POLICY_SCHEMA
    from drover.server.lake.runtime import LakeError, lake_connection

    spec, _, event_id = _legacy_fixture(verified_lake, postgres_control_store)
    expression = (
        "sha256(to_json(struct_pack("
        + ", ".join(f'"{name}" := "{name}"' for name in POLICY_SCHEMA)
        + ")))"
    )
    with lake_connection(spec, read_only=False) as con:
        before_snapshot = con.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchone()[0]
        before_rows = con.execute("SELECT count(*) FROM lake.agent_events").fetchone()[
            0
        ]
        con.execute(
            "INSERT INTO lake.agent_events BY NAME SELECT * REPLACE ("
            + expression
            + " AS _row_sha256) FROM (SELECT * REPLACE "
            "('still-unreceipted' AS id, 'manual:test' AS _import_file) "
            "FROM lake.agent_events WHERE id=?)",
            [event_id],
        )
        before_rows += 1
        before_snapshot = con.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchone()[0]
    with pytest.raises(LakeError, match="deprecation_would_fail_verify"):
        deprecate_legacy(spec, apply=True)
    with lake_connection(spec) as con:
        assert (
            con.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]
            == before_snapshot
        )
        assert (
            con.execute("SELECT count(*) FROM lake.agent_events").fetchone()[0]
            == before_rows
        )


def test_deprecation_keeps_an_archive_watermark_and_survives_export_checkpoint(
    verified_lake, postgres_control_store
):
    import hashlib

    from drover.server.harness.registry import HarnessRegistry
    from drover.server.lake.exporter import LakeOutboxExporter
    from drover.server.lake.fence import MutationFence
    from drover.server.lake.legacy_deprecation import deprecate_legacy
    from drover.server.lake.runtime import lake_connection
    from drover.server.lake.serving_proof import advance_checkpoint, check_proof

    spec, control_path, _ = verified_lake
    spec, _, _ = _legacy_fixture(verified_lake, postgres_control_store)
    report = deprecate_legacy(spec, apply=True)
    with lake_connection(spec) as con:
        assert (
            con.execute(
                "SELECT min(partition_date) FROM lake.import_watermark"
            ).fetchone()
            == con.execute(
                "SELECT min(date) FROM lake.agent_events WHERE date <> '_seed'"
            ).fetchone()
        )
    registry = HarnessRegistry(control_path)
    registry.append_event(
        session_id="export-session",
        event_id="after-deprecation",
        event_type="assistant_output",
        payload={"text": "after deprecation"},
        seq=2,
        content_preview="after deprecation",
    )
    with LakeOutboxExporter(control_path=control_path, spec=spec) as exporter:
        assert exporter.run_once()["acknowledged"] == 1
    digest = hashlib.sha256(
        (spec.data_root / "verification" / "serving-proof.json").read_bytes()
    ).hexdigest()
    with (
        MutationFence(spec.dsn()) as fence,
        lake_connection(spec, read_only=False) as con,
    ):
        checkpoint = advance_checkpoint(spec, digest, fence=fence, con=con)
        assert checkpoint is not None
    with lake_connection(spec) as con:
        assert check_proof(spec, con, digest)["snapshot"] == checkpoint["snapshot"]


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
